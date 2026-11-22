"""Tests for image library id recovery and empty-save guard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app import images


def _jpeg(path: Path) -> None:
    Image.new("RGB", (32, 48), color=(20, 40, 60)).save(path, format="JPEG")


class ImageIdRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.uid = "test-img-uid"
        images._registry.pop(self.uid, None)

    def tearDown(self) -> None:
        images._registry.pop(self.uid, None)
        self._tmpdir.cleanup()

    def test_register_reuses_filename_suffix_as_id(self) -> None:
        folder = self.root / "images"
        folder.mkdir()
        path = folder / "pin_ab12cd34ef.jpg"
        _jpeg(path)
        with mock.patch.object(images, "user_images_dir", return_value=folder), mock.patch.object(
            images, "user_chat_images_dir", return_value=self.root / "chat"
        ), mock.patch.object(images.userstore, "get_kv", return_value={"images": [], "order": []}), mock.patch.object(
            images.userstore, "set_kv"
        ), mock.patch.object(images.storage, "mirror_up"), mock.patch.object(
            images.storage, "rel_of", return_value="images/pin_ab12cd34ef.jpg"
        ):
            images._registry.pop(self.uid, None)
            img = images.register_image(self.uid, path, "upload")
        self.assertEqual(img["id"], "ab12cd34ef")

    def test_recover_image_from_disk_by_suffix(self) -> None:
        folder = self.root / "images"
        folder.mkdir()
        path = folder / "still_deadbeef01.jpg"
        _jpeg(path)
        with mock.patch.object(images, "user_images_dir", return_value=folder), mock.patch.object(
            images, "user_chat_images_dir", return_value=self.root / "chat"
        ), mock.patch.object(images.userstore, "get_kv", return_value={"images": [], "order": []}), mock.patch.object(
            images.userstore, "set_kv"
        ), mock.patch.object(images.storage, "mirror_up"), mock.patch.object(
            images.storage, "ENABLED", False
        ), mock.patch.object(
            images.storage, "rel_of", return_value="images/still_deadbeef01.jpg"
        ):
            images._registry.pop(self.uid, None)
            got = images.get_image(self.uid, "deadbeef01")
        self.assertEqual(got["id"], "deadbeef01")
        self.assertTrue(Path(got["path"]).is_file())

    def test_refuse_empty_save_over_durable(self) -> None:
        durable = {
            "images": [{"id": "keepme1234", "path": "/x.jpg", "name": "x.jpg"}],
            "order": ["keepme1234"],
        }
        writes: list[dict] = []

        def _set(_uid: str, _key: str, data: dict) -> None:
            writes.append(data)

        with mock.patch.object(images.userstore, "get_kv", return_value=durable), mock.patch.object(
            images.userstore, "set_kv", side_effect=_set
        ):
            images._registry[self.uid] = {"images": {}, "order": []}
            images.save_state(self.uid)
        self.assertEqual(writes, [])
        self.assertIn("keepme1234", images._registry[self.uid]["images"])


class MergeImagesKvTests(unittest.TestCase):
    def test_empty_remote_keeps_local(self) -> None:
        from app.userstore import merge_images_kv

        local = {
            "images": [{"id": "a1", "addedAt": 1, "name": "a.jpg"}],
            "order": ["a1"],
        }
        remote = {"images": [], "order": []}
        merged = merge_images_kv(local, remote)
        self.assertEqual(len(merged["images"]), 1)
        self.assertEqual(merged["images"][0]["id"], "a1")


if __name__ == "__main__":
    unittest.main()
