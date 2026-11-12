"""ZIP export must keep asset identity, order and all-or-nothing guarantees."""
import io
import json
import time
import zipfile

import pytest
from PIL import Image


COLORS = {
    "red.png": (255, 0, 0, 255),
    "green.png": (0, 255, 0, 255),
    "blue.png": (0, 0, 255, 255),
}


def upload_color(client, name, color, size=(24, 18)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, name)})
    assert response.status_code == 201, response.json
    return response.json["data"]


def upload_many(client):
    red = upload_color(client, "red.png", COLORS["red.png"])
    green = upload_color(client, "green.png", COLORS["green.png"], size=(40, 30))
    blue = upload_color(client, "blue.png", COLORS["blue.png"], size=(12, 12))
    return red, green, blue


def archive_request(client, asset_ids, format_name="PNG"):
    return client.post("/api/exports/archive", json={
        "asset_ids": asset_ids,
        "options": {"format": format_name},
    })


def read_archive(client, record):
    content = client.get(record["url"]).data
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        images = {}
        for name in archive.namelist():
            if name == "manifest.json":
                continue
            with Image.open(io.BytesIO(archive.read(name))) as image:
                images[name] = image.convert("RGBA").getpixel((0, 0))
        return manifest, images


def export_state(app):
    services = app.extensions["studio"]
    return len(services["store"].all("exports")), sorted(p.name for p in services["settings"].exports.iterdir())


def test_archive_order_and_content_ignore_completion_order(app, client, monkeypatch):
    red, green, blue = upload_many(client)
    service = app.extensions["studio"]["exports"]
    encoded = service.encode

    def delayed_encode(image, options):
        # Make the first selected asset finish last so completion order is
        # reversed compared with selection order.
        if image.size == (24, 18):
            time.sleep(0.25)
        payload = encoded(image, options)
        assert Image.open(io.BytesIO(payload)).convert("RGBA").getpixel((0, 0)) is not None
        return payload

    monkeypatch.setattr(service, "encode", delayed_encode)

    response = archive_request(client, [red["id"], green["id"], blue["id"]])
    assert response.status_code == 201, response.json
    record = response.json["data"]
    manifest, images = read_archive(client, record)

    assert manifest["assets"] == [red["id"], green["id"], blue["id"]]
    assert manifest["count"] == 3
    assert [entry["asset_id"] for entry in manifest["entries"]] == [red["id"], green["id"], blue["id"]]
    assert [entry["source_name"] for entry in manifest["entries"]] == ["red.png", "green.png", "blue.png"]

    expected = [COLORS["red.png"], COLORS["green.png"], COLORS["blue.png"]]
    for index, entry in enumerate(manifest["entries"], start=1):
        assert entry["index"] == index
        assert entry["filename"].startswith(f"{index:02d}_")
        assert entry["filename"].endswith(".png")
        assert entry["bytes"] > 0
        assert images[entry["filename"]] == expected[index - 1]

    assert len(images) == manifest["count"]
    assert len(images) == len(manifest["entries"])


def test_archive_deduplicates_first_selection_order(app, client):
    red, green, blue = upload_many(client)
    response = archive_request(client, [blue["id"], red["id"], blue["id"], red["id"]])
    assert response.status_code == 201, response.json
    manifest, images = read_archive(client, response.json["data"])

    assert manifest["assets"] == [blue["id"], red["id"]]
    assert manifest["count"] == 2
    assert len(images) == 2
    assert [entry["source_name"] for entry in manifest["entries"]] == ["blue.png", "red.png"]
    assert images[manifest["entries"][0]["filename"]] == COLORS["blue.png"]
    assert images[manifest["entries"][1]["filename"]] == COLORS["red.png"]


def test_archive_unknown_member_rejects_whole_batch(app, client):
    red, green, _ = upload_many(client)
    before = export_state(app)
    response = archive_request(client, [red["id"], "0" * 32, green["id"]])
    assert response.status_code == 404
    assert response.json["error"]["details"]["asset_id"] == "0" * 32
    assert export_state(app) == before


def test_archive_missing_file_rejects_whole_batch(app, client):
    red, green, blue = upload_many(client)
    (app.extensions["studio"]["settings"].media / f"{green['id']}.png").unlink()
    before = export_state(app)
    response = archive_request(client, [red["id"], green["id"], blue["id"]])
    assert response.status_code == 410
    assert response.json["error"]["details"]["asset_id"] == green["id"]
    assert export_state(app) == before


def test_archive_encoding_failure_rejects_whole_batch(app, client, monkeypatch):
    red, green, blue = upload_many(client)
    service = app.extensions["studio"]["exports"]
    encoded = service.encode

    def failing_encode(image, options):
        if image.size == (40, 30):
            raise RuntimeError("boom")
        return encoded(image, options)

    monkeypatch.setattr(service, "encode", failing_encode)
    before = export_state(app)
    response = archive_request(client, [red["id"], green["id"], blue["id"]])
    assert response.status_code == 500
    assert response.json["error"]["details"]["asset_id"] == green["id"]
    assert export_state(app) == before


def test_archive_writes_no_record_or_file_after_failure(app, client, monkeypatch):
    red, green, _ = upload_many(client)
    service = app.extensions["studio"]["exports"]

    def failing_encode(image, options):
        if image.size == (24, 18):
            raise RuntimeError("boom")
        return b"never written"

    monkeypatch.setattr(service, "encode", failing_encode)
    before = export_state(app)
    response = archive_request(client, [red["id"], green["id"]])
    assert response.status_code == 500
    assert export_state(app) == before
    assert list(app.extensions["studio"]["settings"].exports.iterdir()) == []


def test_single_image_archive_still_works(client):
    red = upload_color(client, "red.png", COLORS["red.png"])
    response = archive_request(client, [red["id"]])
    assert response.status_code == 201, response.json
    manifest, images = read_archive(client, response.json["data"])
    assert manifest["assets"] == [red["id"]]
    assert manifest["count"] == 1
    assert len(images) == 1
    assert images[manifest["entries"][0]["filename"]] == COLORS["red.png"]


def test_single_image_export_still_works(client):
    red = upload_color(client, "red.png", COLORS["red.png"])
    response = client.post("/api/exports", json={"asset_id": red["id"], "options": {"format": "JPEG"}})
    assert response.status_code == 201, response.json
    with Image.open(io.BytesIO(client.get(response.json["data"]["url"]).data)) as image:
        assert image.format == "JPEG"
        assert image.size == (24, 18)
