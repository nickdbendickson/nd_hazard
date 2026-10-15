"""Carrying the pack on a USB stick (1.4.0): the checksum file is the whole point, so the tests
are about what happens when a file does not match it, and about never destroying the pack that
is already at the destination."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import packmove as PM  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


def _pack(folder, extra=None):
    os.makedirs(os.path.join(folder, "hydro"), exist_ok=True)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"format": "nd-dam-suite-pack", "datasets": {"A": {"count": 1}}}, f)
    with open(os.path.join(folder, "hydro", "3DHP_flowlines.gpkg"), "wb") as f:
        f.write(b"flowlines" * 1000)
    with open(os.path.join(folder, "LAYERS.txt"), "w", encoding="utf-8") as f:
        f.write("one layer\n")
    for name, data in (extra or {}).items():
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
    return folder


@test
def test_is_pack_asks_for_the_manifest():
    with tempfile.TemporaryDirectory() as d:
        assert PM.is_pack(d) is False
        assert PM.is_pack(_pack(d)) is True
        assert PM.is_pack("") is False


@test
def test_pack_files_leaves_out_working_files_and_the_checksum_file():
    with tempfile.TemporaryDirectory() as d:
        _pack(d, {"big.gpkg.part": b"half a download", PM.SUMS_NAME: b"stale"})
        rels = [rel for rel, _size in PM.pack_files(d)]
        assert rels == ["LAYERS.txt", "hydro/3DHP_flowlines.gpkg", "manifest.json"]


@test
def test_sums_round_trip_in_sha256sum_format():
    with tempfile.TemporaryDirectory() as d:
        _pack(d)
        sums = PM.checksums(d)
        text = PM.sums_text(sums)
        assert text.splitlines()[0].split()[1] == "LAYERS.txt"
        assert len(text.splitlines()[0].split()[0]) == 64
        assert PM.parse_sums(text) == sums
        assert PM.parse_sums("# a comment\nnot a line\n") == {}


@test
def test_checksums_report_progress_in_bytes():
    with tempfile.TemporaryDirectory() as d:
        _pack(d)
        seen = []
        PM.checksums(d, progress=lambda done, total: seen.append((done, total)))
        assert seen and seen[-1][0] == seen[-1][1] == PM.total_size(d)


@test
def test_verify_catches_a_damaged_copy_and_an_added_file():
    with tempfile.TemporaryDirectory() as d:
        _pack(d)
        PM.write_sums(d)
        assert PM.verify(d) == (True, [])
        with open(os.path.join(d, "LAYERS.txt"), "a", encoding="utf-8") as f:
            f.write("a line the stick lost half of\n")
        ok, bad = PM.verify(d)
        assert ok is False
        assert bad == [("LAYERS.txt", "different contents (the copy is damaged)")]
        os.remove(os.path.join(d, "LAYERS.txt"))
        ok, bad = PM.verify(d)
        assert bad == [("LAYERS.txt", "missing from the copy")]


@test
def test_verify_without_a_checksum_file_is_not_a_pass():
    with tempfile.TemporaryDirectory() as d:
        _pack(d)
        ok, bad = PM.verify(d)
        assert ok is False and "no checksum file" in bad[0][1]


@test
def test_copy_pack_writes_the_data_and_its_checksums():
    with tempfile.TemporaryDirectory() as d:
        pack = _pack(os.path.join(d, "ND_Dam_Suite_Pack"))
        usb = os.path.join(d, "usb")
        os.makedirs(usb)
        seen = []
        out = PM.copy_pack(pack, usb, progress=lambda a, b: seen.append(a))
        assert out["folder"] == os.path.join(usb, "ND_Dam_Suite_Pack")
        assert out["files"] == 3 and out["bytes"] == PM.total_size(pack)
        assert seen
        assert PM.verify(out["folder"]) == (True, [])
        assert not os.path.exists(out["folder"] + ".incoming")


@test
def test_copy_pack_refuses_a_folder_that_is_not_a_pack():
    with tempfile.TemporaryDirectory() as d:
        try:
            PM.copy_pack(d, os.path.join(d, "out"))
        except PM.PackMoveError as e:
            assert "not a pack folder" in str(e)
        else:
            raise AssertionError("a folder with no manifest is not a pack")


@test
def test_copy_pack_keeps_the_old_copy_when_it_is_cancelled():
    with tempfile.TemporaryDirectory() as d:
        pack = _pack(os.path.join(d, "ND_Dam_Suite_Pack"))
        usb = os.path.join(d, "usb")
        os.makedirs(usb)
        first = PM.copy_pack(pack, usb)
        try:
            PM.copy_pack(pack, usb, cancelled=lambda: True)
        except PM.PackMoveError as e:
            assert "cancelled" in str(e)
        else:
            raise AssertionError("a cancel must stop the copy")
        assert PM.verify(first["folder"]) == (True, [])        # the good copy is untouched
        assert not os.path.exists(first["folder"] + ".incoming")


@test
def test_import_pack_verifies_before_it_replaces_anything():
    with tempfile.TemporaryDirectory() as d:
        office = _pack(os.path.join(d, "office", "ND_Dam_Suite_Pack"))
        usb = os.path.join(d, "usb")
        os.makedirs(usb)
        stick = PM.copy_pack(office, usb)["folder"]
        laptop = os.path.join(d, "laptop")
        os.makedirs(laptop)
        out = PM.import_pack(stick, laptop)
        assert out["folder"] == os.path.join(laptop, "ND_Dam_Suite_Pack")
        assert out["checked"] is True
        assert PM.verify(out["folder"]) == (True, [])
        # now damage the stick and import again over a good pack
        with open(os.path.join(stick, "hydro", "3DHP_flowlines.gpkg"), "wb") as f:
            f.write(b"truncated")
        try:
            PM.import_pack(stick, laptop)
        except PM.PackMoveError as e:
            assert "damaged" in str(e) and "Nothing was changed" in str(e)
        else:
            raise AssertionError("a damaged stick must not be imported")
        assert PM.verify(out["folder"]) == (True, [])          # the laptop's pack survived


@test
def test_import_pack_refuses_a_pack_with_no_checksums_unless_asked():
    with tempfile.TemporaryDirectory() as d:
        stick = _pack(os.path.join(d, "stick", "ND_Dam_Suite_Pack"))
        laptop = os.path.join(d, "laptop")
        os.makedirs(laptop)
        try:
            PM.import_pack(stick, laptop)
        except PM.PackMoveError as e:
            assert PM.SUMS_NAME in str(e)
        else:
            raise AssertionError("no checksum file must be refused by default")
        out = PM.import_pack(stick, laptop, check=False)
        assert out["checked"] is False and PM.is_pack(out["folder"])


@test
def test_import_pack_will_not_import_a_pack_into_its_own_folder():
    with tempfile.TemporaryDirectory() as d:
        pack = _pack(os.path.join(d, "ND_Dam_Suite_Pack"))
        try:
            PM.import_pack(pack, d, check=False)
        except PM.PackMoveError as e:
            assert "already in this folder" in str(e)
        else:
            raise AssertionError("importing a pack onto itself must be refused")
