"""Unit and integration tests for server.py.

Run from the project folder:  python3 -m unittest discover -s tests -v

The HTTP tests start the real server on a free port and talk to it over a socket.
"""
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_text(path, text="x"):
    with open(path, "w") as f:
        f.write(text)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class SafeNameTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(server.safe_name("photo.jpg"), "photo.jpg")

    def test_strips_directories(self):
        self.assertEqual(server.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(server.safe_name("a/b/c.txt"), "c.txt")

    def test_decodes_percent_encoding_before_stripping(self):
        self.assertEqual(server.safe_name("..%2F..%2Fsecret.txt"), "secret.txt")

    def test_rejects_empty_and_dots(self):
        for bad in ("", " ", ".", "..", "dir/", "../"):
            self.assertIsNone(server.safe_name(bad), bad)


class DeviceNameTests(unittest.TestCase):
    def test_iphone_safari(self):
        self.assertEqual(server.device_name(IPHONE), "iPhone · Safari")

    def test_android_chrome(self):
        self.assertEqual(server.device_name(ANDROID), "Android · Chrome")

    def test_unknown_and_missing(self):
        self.assertEqual(server.device_name(None), "Phone")
        self.assertEqual(server.device_name(""), "Phone")


class UniquePathTests(TmpDirCase):
    def test_free_name_is_unchanged(self):
        self.assertEqual(server.unique_path(self.tmp, "a.txt"), os.path.join(self.tmp, "a.txt"))

    def test_existing_file_gets_numbered(self):
        write_text(os.path.join(self.tmp, "a.txt"))
        self.assertEqual(server.unique_path(self.tmp, "a.txt"), os.path.join(self.tmp, "a (1).txt"))

    def test_in_progress_part_file_counts_as_taken(self):
        write_text(os.path.join(self.tmp, "a.txt.part"))
        self.assertEqual(server.unique_path(self.tmp, "a.txt"), os.path.join(self.tmp, "a (1).txt"))


class ReserveTests(TmpDirCase):
    def test_reserves_a_part_file_and_returns_final_name(self):
        dest, fd = server.reserve(self.tmp, "a.txt")
        os.close(fd)
        self.assertEqual(dest, os.path.join(self.tmp, "a.txt"))
        self.assertTrue(os.path.exists(dest + ".part"))
        self.assertFalse(os.path.exists(dest))

    def test_second_reservation_of_same_name_gets_a_new_name(self):
        d1, fd1 = server.reserve(self.tmp, "a.txt")
        d2, fd2 = server.reserve(self.tmp, "a.txt")
        os.close(fd1), os.close(fd2)
        self.assertNotEqual(d1, d2)
        self.assertEqual(os.path.basename(d2), "a (1).txt")

    def test_skips_names_of_finished_files(self):
        write_text(os.path.join(self.tmp, "a.txt"))
        dest, fd = server.reserve(self.tmp, "a.txt")
        os.close(fd)
        self.assertEqual(os.path.basename(dest), "a (1).txt")

    def test_concurrent_reservations_never_collide(self):
        results, lock = [], threading.Lock()

        def worker():
            dest, fd = server.reserve(self.tmp, "same.bin")
            os.close(fd)
            with lock:
                results.append(dest)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 25)
        self.assertEqual(len(set(results)), 25)


class CopyInTests(TmpDirCase):
    def make_src(self, data=b"hello world" * 1000):
        src = os.path.join(self.tmp, "src.bin")
        with open(src, "wb") as f:
            f.write(data)
        return src, data

    def test_copy_is_complete_and_leaves_no_part_file(self):
        src, data = self.make_src()
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        dest = server.copy_in(src, out)
        self.assertEqual(read_bytes(dest), data)
        self.assertEqual(os.listdir(out), ["src.bin"])

    def test_file_is_hidden_until_the_copy_finishes(self):
        src, _ = self.make_src(b"x" * 10)
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        dest, fd = server.reserve(out, "src.bin")  # what copy_in holds while copying
        os.close(fd)
        self.assertEqual(server.list_dir(out), [])
        self.assertTrue(server.is_busy(out))
        os.replace(dest + ".part", dest)
        self.assertEqual([f["name"] for f in server.list_dir(out)], ["src.bin"])
        self.assertFalse(server.is_busy(out))

    def test_copies_of_same_file_get_distinct_names(self):
        src, data = self.make_src()
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        threads = [threading.Thread(target=server.copy_in, args=(src, out)) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        names = sorted(os.listdir(out))
        self.assertEqual(len(names), 6)
        self.assertFalse([n for n in names if n.endswith(".part")])
        for n in names:
            self.assertEqual(read_bytes(os.path.join(out, n)), data)

    def test_failed_copy_removes_its_part_file(self):
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        with self.assertRaises(FileNotFoundError):
            server.copy_in(os.path.join(self.tmp, "missing.bin"), out)
        self.assertEqual(os.listdir(out), [])


class ListingTests(TmpDirCase):
    def touch(self, *parts, data=b"x"):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def test_list_dir_hides_part_and_hidden_files(self):
        self.touch("a.txt")
        self.touch("b.txt.part")
        self.touch(".DS_Store")
        self.assertEqual([f["name"] for f in server.list_dir(self.tmp)], ["a.txt"])

    def test_list_tree_groups_folders_and_sums_sizes(self):
        self.touch("loose.txt", data=b"12")
        self.touch("Trip", "a.jpg", data=b"1234")
        self.touch("Trip", "b.jpg", data=b"123456")
        tree = server.list_tree(self.tmp)
        self.assertEqual([f["name"] for f in tree["files"]], ["loose.txt"])
        trip = tree["folders"][0]
        self.assertEqual((trip["name"], trip["size"], len(trip["files"])), ("Trip", 10, 2))

    def test_folder_with_only_unfinished_files_is_not_listed(self):
        self.touch("Trip", "a.jpg.part")
        self.assertEqual(server.list_tree(self.tmp)["folders"], [])

    def test_folder_is_busy_while_a_part_file_exists(self):
        self.touch("Trip", "a.jpg")
        self.touch("Trip", "b.jpg.part")
        self.assertTrue(server.list_tree(self.tmp)["folders"][0]["busy"])
        os.remove(os.path.join(self.tmp, "Trip", "b.jpg.part"))
        self.assertFalse(server.list_tree(self.tmp)["folders"][0]["busy"])

    def test_target_dir_creates_named_folder_and_ignores_bad_names(self):
        p = server.target_dir(self.tmp, "Holiday")
        self.assertEqual(p, os.path.join(self.tmp, "Holiday"))
        self.assertTrue(os.path.isdir(p))
        self.assertEqual(server.target_dir(self.tmp, ""), self.tmp)
        self.assertEqual(server.target_dir(self.tmp, None), self.tmp)
        self.assertEqual(server.target_dir(self.tmp, ".."), self.tmp)
        self.assertEqual(server.target_dir(self.tmp, "../escape"), os.path.join(self.tmp, "escape"))


class PinTests(unittest.TestCase):
    def test_new_pin_is_four_digits_and_resets_failures(self):
        server.STATE["fails"] = 3
        for _ in range(50):
            server.new_pin()
            self.assertRegex(server.STATE["pin"], r"^\d{4}$")
        self.assertEqual(server.STATE["fails"], 0)


class QrTests(unittest.TestCase):
    def test_matrix_is_square_of_zeros_and_ones(self):
        m = server.qr_matrix("http://192.168.1.5:8000/?t=abc")
        self.assertTrue(len(m) > 20)
        self.assertTrue(all(len(row) == len(m) for row in m))
        self.assertTrue(all(v in (0, 1) for row in m for v in row))


class ServerCase(unittest.TestCase):
    """Starts the real server once per test class and gives helpers to talk to it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.httpd, cls.port = server.start(os.path.join(cls.tmp, "share"), free_port())
        cls.token = urllib.parse.parse_qs(urllib.parse.urlparse(server.STATE["url"]).query)["t"][0]
        cls.inbox, cls.outbox = server.STATE["inbox"], server.STATE["outbox"]
        cls.httpd.handle_error = lambda *a: None  # clients that hang up mid-request are expected here

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for box in (self.inbox, self.outbox):
            for n in os.listdir(box):
                p = os.path.join(box, n)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        server.STATE["chat"] = []

    def req(self, method, path, headers=None, body=None, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, r.getheader("Set-Cookie"), r.read()
        finally:
            c.close()

    def pair(self, ua=IPHONE):
        """Scan + PIN; returns headers for an authenticated phone."""
        st, ck, _ = self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, server.STATE["pin"])
        self.assertEqual(st, 204)
        return {"Cookie": ck.split(";")[0], "User-Agent": ua}

    def put_file(self, auth, name, data, folder=None):
        path = "/upload/" + urllib.parse.quote(name) + ("?folder=" + urllib.parse.quote(folder) if folder else "")
        return self.req("PUT", path, auth, data)

    def raw(self, request_bytes):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request_bytes)
            return s.recv(200).split(b"\r\n")[0].decode()


class PairingTests(ServerCase):
    def test_no_cookies_means_scan_first(self):
        st, _, body = self.req("GET", "/api/files")
        self.assertEqual((st, body), (401, b"scan"))

    def test_scanning_qr_asks_for_pin(self):
        st, ck, _ = self.req("GET", f"/?t={self.token}")
        self.assertEqual(st, 302)
        self.assertIn(f"q={self.token}", ck)
        st, _, body = self.req("GET", "/api/files", {"Cookie": f"q={self.token}"})
        self.assertEqual((st, body), (401, b"pin"))

    def test_wrong_token_in_qr_link_is_not_accepted(self):
        st, ck, _ = self.req("GET", "/?t=wrong")
        self.assertEqual(st, 200)  # just the page, no cookie
        self.assertIsNone(ck)

    def test_pin_without_scanning_first_is_refused(self):
        st, _, _ = self.req("POST", "/pair", None, server.STATE["pin"])
        self.assertEqual(st, 401)

    def test_wrong_pin_is_refused(self):
        wrong = "0000" if server.STATE["pin"] != "0000" else "1111"
        st, ck, _ = self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, wrong)
        self.assertEqual(st, 403)
        self.assertIsNone(ck)

    def test_qr_token_alone_is_not_a_login(self):
        """The PIN cannot be skipped by sending the token from the QR link as the login cookie."""
        st, _, _ = self.req("GET", "/api/files", {"Cookie": f"t={self.token}"})
        self.assertEqual(st, 401)
        st, _, _ = self.req("GET", "/files/x.txt", {"Cookie": f"t={self.token}"})
        self.assertEqual(st, 401)
        st, _, _ = self.put_file({"Cookie": f"t={self.token}"}, "x.txt", b"x")
        self.assertEqual(st, 401)

    def test_correct_pin_gives_a_separate_session_token(self):
        auth = self.pair()
        sid = auth["Cookie"][2:]
        self.assertNotEqual(sid, self.token)
        self.assertGreaterEqual(len(sid), 32)
        self.assertEqual(self.req("GET", "/api/files", auth)[0], 200)

    def test_session_cookie_is_httponly_and_samesite(self):
        _, ck, _ = self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, server.STATE["pin"])
        self.assertIn("HttpOnly", ck)
        self.assertIn("SameSite=Strict", ck)

    def test_each_pairing_gets_a_different_token(self):
        self.assertNotEqual(self.pair()["Cookie"], self.pair()["Cookie"])

    def test_pin_works_only_once(self):
        server.STATE["pin"] = "1234"
        with mock.patch.object(server.secrets, "randbelow", return_value=7):  # next PIN will be 0007
            self.assertEqual(self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, "1234")[0], 204)
        self.assertEqual(server.STATE["pin"], "0007")
        self.assertEqual(self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, "1234")[0], 403)

    def test_five_wrong_pins_replace_the_pin(self):
        server.new_pin()
        old = server.STATE["pin"]
        wrong = "".join(str((int(c) + 1) % 10) for c in old)  # always different from the real PIN
        codes = [self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, wrong)[0] for _ in range(5)]
        self.assertEqual(codes[:4], [403] * 4)
        self.assertEqual(codes[4], 429)
        self.assertEqual(server.STATE["fails"], 0)


class HostAndOriginTests(ServerCase):
    def test_unknown_host_is_rejected_everywhere(self):
        for method, path in (("GET", "/desktop/info"), ("GET", "/"), ("POST", "/desktop/chat"), ("POST", "/pair")):
            st, _, _ = self.req(method, path, host="evil.example:%d" % self.port)
            self.assertEqual(st, 403, (method, path))

    def test_host_with_wrong_port_is_rejected(self):
        self.assertEqual(self.req("GET", "/desktop/info", host="127.0.0.1:1")[0], 403)

    def test_localhost_and_loopback_hosts_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self.assertEqual(self.req("GET", "/desktop/info", host=host)[0], 200, host)

    def test_phone_can_use_the_address_from_the_qr_code(self):
        qr_host = server.STATE["url"].split("/")[2]
        auth = self.pair()
        self.assertEqual(self.req("GET", "/api/files", auth, host=qr_host)[0], 200)

    def test_foreign_origin_cannot_post_chat_as_pc(self):
        st, _, _ = self.req("POST", "/desktop/chat", {"Origin": "http://evil.example"}, "click here")
        self.assertEqual(st, 403)
        self.assertEqual(server.STATE["chat"], [])

    def test_own_origin_and_no_origin_can_post(self):
        self.assertEqual(self.req("POST", "/desktop/chat", {"Origin": f"http://127.0.0.1:{self.port}"}, "hi")[0], 204)
        self.assertEqual(self.req("POST", "/desktop/chat", None, "hi again")[0], 204)

    def test_foreign_origin_cannot_upload_even_when_logged_in(self):
        auth = self.pair()
        st, _, _ = self.put_file({**auth, "Origin": "http://evil.example"}, "x.txt", b"x")
        self.assertEqual(st, 403)
        self.assertEqual(os.listdir(self.inbox), [])

    def test_desktop_endpoints_are_for_this_pc_only(self):
        # Every request in these tests comes from 127.0.0.1, so check the rule directly.
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ("192.168.1.50", 5555)
        self.assertFalse(handler.is_local())
        handler.client_address = ("127.0.0.1", 5555)
        self.assertTrue(handler.is_local())


class UploadTests(ServerCase):
    def test_upload_requires_login(self):
        self.assertEqual(self.put_file({}, "a.txt", b"x")[0], 401)

    def test_upload_saves_file(self):
        st, _, body = self.put_file(self.pair(), "a.txt", b"hello")
        self.assertEqual((st, body), (201, b"a.txt"))
        self.assertEqual(read_bytes(os.path.join(self.inbox, "a.txt")), b"hello")
        self.assertEqual([n for n in os.listdir(self.inbox) if n.endswith(".part")], [])

    def test_upload_into_named_folder(self):
        self.put_file(self.pair(), "a.txt", b"1", folder="Trip")
        self.assertEqual(os.listdir(os.path.join(self.inbox, "Trip")), ["a.txt"])

    def test_path_traversal_in_name_and_folder_is_neutralised(self):
        auth = self.pair()
        self.req("PUT", "/upload/..%2F..%2Fevil.txt", auth, b"x")
        self.put_file(auth, "ok.txt", b"x", folder="../../outside")
        self.assertTrue(os.path.exists(os.path.join(self.inbox, "evil.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.inbox, "outside", "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside")))

    def test_same_name_twice_is_numbered(self):
        auth = self.pair()
        self.assertEqual(self.put_file(auth, "a.txt", b"1")[2], b"a.txt")
        self.assertEqual(self.put_file(auth, "a.txt", b"2")[2], b"a (1).txt")

    def test_simultaneous_uploads_with_same_name_do_not_corrupt_each_other(self):
        auth = self.pair()
        results, lock = [], threading.Lock()

        def up(i):
            data = bytes([i]) * 500_000
            _, _, name = self.put_file(auth, "IMG.jpg", data)
            with lock:
                results.append((name.decode(), data))

        threads = [threading.Thread(target=up, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len({n for n, _ in results}), 8)
        for name, data in results:
            self.assertEqual(read_bytes(os.path.join(self.inbox, name)), data)
        self.assertEqual([n for n in os.listdir(self.inbox) if n.endswith(".part")], [])

    def test_upload_without_content_length_is_rejected(self):
        auth = self.pair()
        line = self.raw(f"PUT /upload/x.bin HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{'Cookie: ' + auth['Cookie']}\r\n\r\n".encode())
        self.assertIn("411", line)
        self.assertEqual(os.listdir(self.inbox), [])

    def test_upload_with_bad_content_length_is_rejected(self):
        auth = self.pair()
        for bad in ("abc", "-5", "1.5"):
            line = self.raw((f"PUT /upload/x.bin HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                             f"Cookie: {auth['Cookie']}\r\nContent-Length: {bad}\r\n\r\n").encode())
            self.assertIn("411", line, bad)
        self.assertEqual(os.listdir(self.inbox), [])

    def test_dropped_upload_leaves_no_part_file_behind(self):
        auth = self.pair()
        cleaned, real_remove = threading.Event(), os.remove

        def spy(path, *a, **k):
            real_remove(path, *a, **k)
            if str(path).endswith("cut.bin.part"):
                cleaned.set()

        with mock.patch.object(os, "remove", spy):
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
                s.sendall((f"PUT /upload/cut.bin?folder=Cut HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                           f"Cookie: {auth['Cookie']}\r\nContent-Length: 5000000\r\n\r\n").encode() + b"x" * 1000)
            self.assertTrue(cleaned.wait(10), "server never cleaned up the dropped upload")
        self.assertEqual(os.listdir(os.path.join(self.inbox, "Cut")), [])

    def test_inbox_folder_shows_busy_while_a_file_is_still_arriving(self):
        auth = self.pair()
        self.put_file(auth, "one.txt", b"1", folder="Trip")
        part = os.path.join(self.inbox, "Trip", "two.txt.part")
        write_text(part, "partial")
        info = json.loads(self.req("GET", "/desktop/info")[2])
        self.assertEqual([(f["name"], f["busy"]) for f in info["inbox"]["folders"]], [("Trip", True)])
        os.remove(part)
        info = json.loads(self.req("GET", "/desktop/info")[2])
        self.assertFalse(info["inbox"]["folders"][0]["busy"])


class DownloadTests(ServerCase):
    def stage(self, name, data=b"payload", folder=None):
        d = server.target_dir(self.outbox, folder)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)

    def test_download_requires_login(self):
        self.stage("a.txt")
        self.assertEqual(self.req("GET", "/files/a.txt")[0], 401)

    def test_list_and_download_file(self):
        self.stage("a.txt", b"hello")
        auth = self.pair()
        listing = json.loads(self.req("GET", "/api/files", auth)[2])
        self.assertEqual([f["name"] for f in listing["files"]], ["a.txt"])
        st, _, body = self.req("GET", "/files/a.txt", auth)
        self.assertEqual((st, body), (200, b"hello"))

    def test_download_missing_file_is_404(self):
        self.assertEqual(self.req("GET", "/files/nope.txt", self.pair())[0], 404)

    def test_download_cannot_escape_the_outbox(self):
        secret = os.path.join(self.tmp, "secret.txt")
        write_text(secret, "top secret")
        auth = self.pair()
        self.assertEqual(self.req("GET", "/files/..%2Fsecret.txt", auth)[0], 404)
        self.assertEqual(self.req("GET", "/files/secret.txt?folder=..", auth)[0], 404)

    def test_download_from_named_folder(self):
        self.stage("a.txt", b"in folder", folder="Trip")
        st, _, body = self.req("GET", "/files/a.txt?folder=Trip", self.pair())
        self.assertEqual((st, body), (200, b"in folder"))

    def test_half_copied_file_is_invisible_and_not_downloadable(self):
        write_text(os.path.join(self.outbox, "video.mp4.part"), "partial")
        auth = self.pair()
        self.assertEqual(json.loads(self.req("GET", "/api/files", auth)[2])["files"], [])
        self.assertEqual(self.req("GET", "/files/video.mp4", auth)[0], 404)

    def test_zip_download(self):
        import io
        import zipfile
        self.stage("a.txt", b"AAA", folder="Trip")
        self.stage("b.txt", b"BBB", folder="Trip")
        st, _, body = self.req("GET", "/zip/Trip", self.pair())
        self.assertEqual(st, 200)
        z = zipfile.ZipFile(io.BytesIO(body))
        self.assertEqual(sorted(z.namelist()), ["Trip/a.txt", "Trip/b.txt"])
        self.assertEqual(z.read("Trip/a.txt"), b"AAA")

    def test_zip_is_refused_while_folder_is_still_being_copied(self):
        self.stage("a.txt", folder="Trip")
        part = os.path.join(self.outbox, "Trip", "big.mov.part")
        write_text(part, "partial")
        auth = self.pair()
        listing = json.loads(self.req("GET", "/api/files", auth)[2])
        self.assertTrue(listing["folders"][0]["busy"])
        self.assertEqual(self.req("GET", "/zip/Trip", auth)[0], 409)
        os.remove(part)
        self.assertEqual(self.req("GET", "/zip/Trip", auth)[0], 200)

    def test_zip_of_missing_folder_is_404(self):
        self.assertEqual(self.req("GET", "/zip/Nope", self.pair())[0], 404)


class ChatTests(ServerCase):
    def test_phone_and_pc_messages_round_trip(self):
        auth = self.pair(ANDROID)
        self.assertEqual(self.req("POST", "/chat", auth, "from phone")[0], 204)
        self.assertEqual(self.req("POST", "/desktop/chat", None, "from pc")[0], 204)
        msgs = json.loads(self.req("GET", "/desktop/chat?since=0")[2])
        self.assertEqual([(m["from"], m["text"]) for m in msgs], [("phone", "from phone"), ("pc", "from pc")])
        self.assertEqual(msgs[0]["name"], "Android · Chrome")
        self.assertEqual([m["id"] for m in msgs], [1, 2])

    def test_since_returns_only_newer_messages(self):
        for t in ("a", "b", "c"):
            self.req("POST", "/desktop/chat", None, t)
        msgs = json.loads(self.req("GET", "/desktop/chat?since=2")[2])
        self.assertEqual([m["text"] for m in msgs], ["c"])

    def test_phone_chat_requires_login(self):
        self.assertEqual(self.req("POST", "/chat", None, "hi")[0], 401)
        self.assertEqual(self.req("GET", "/chat?since=0")[0], 401)

    def test_empty_message_is_rejected(self):
        self.assertEqual(self.req("POST", "/desktop/chat", None, "   \n")[0], 400)

    def test_message_is_trimmed_and_capped(self):
        self.req("POST", "/desktop/chat", None, "x" * 9000 + "   \n")
        text = json.loads(self.req("GET", "/desktop/chat?since=0")[2])[0]["text"]
        self.assertEqual(len(text), 5000)

    def test_only_last_200_messages_are_kept(self):
        for i in range(205):
            self.req("POST", "/desktop/chat", None, str(i))
        self.assertEqual(len(server.STATE["chat"]), 200)


class MalformedInputTests(ServerCase):
    def test_bad_since_value_is_a_400(self):
        self.assertEqual(self.req("GET", "/desktop/chat?since=abc")[0], 400)
        self.assertEqual(self.req("GET", "/chat?since=1.5", self.pair())[0], 400)

    def test_non_utf8_pin_is_refused_not_a_crash(self):
        st, _, _ = self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, b"\xff\xfe\xfd")
        self.assertEqual(st, 403)

    def test_bad_content_length_on_post_is_a_400(self):
        auth = self.pair()
        for bad in ("abc", "-5"):
            st, _, _ = self.req("POST", "/chat", {**auth, "Content-Length": bad})
            self.assertEqual(st, 400, bad)
            line = self.raw((f"POST /pair HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                             f"Cookie: q={self.token}\r\nContent-Length: {bad}\r\n\r\n").encode())
            self.assertIn("400", line, bad)

    def test_unknown_paths_are_404(self):
        self.assertEqual(self.req("GET", "/nothing", self.pair())[0], 404)
        self.assertEqual(self.req("GET", "/desktop/nothing")[0], 404)
        self.assertEqual(self.req("POST", "/nothing", None, "x")[0], 404)

    def test_server_still_works_after_bad_requests(self):
        self.req("GET", "/desktop/chat?since=abc")
        self.assertEqual(self.req("GET", "/desktop/info")[0], 200)


class DeviceInfoTests(ServerCase):
    def test_connected_phones_show_readable_names(self):
        server.STATE["seen"], server.STATE["names"] = {}, {}
        auth = self.pair(IPHONE)
        self.req("GET", "/api/files", auth)
        info = json.loads(self.req("GET", "/desktop/info")[2])
        self.assertEqual([p["name"] for p in info["connected"]], ["iPhone · Safari"])

    def test_phone_can_read_the_pc_name(self):
        data = json.loads(self.req("GET", "/api/info", self.pair())[2])
        self.assertEqual(data["host"], socket.gethostname())

    def test_pc_info_has_qr_pin_and_url(self):
        info = json.loads(self.req("GET", "/desktop/info")[2])
        self.assertEqual(info["pin"], server.STATE["pin"])
        self.assertTrue(info["url"].startswith("http://"))
        self.assertGreater(len(info["qr"]), 20)


class StartupTests(TmpDirCase):
    def test_leftovers_from_a_previous_run_are_archived(self):
        share = os.path.join(self.tmp, "share")
        os.makedirs(os.path.join(share, "From phone"))
        write_text(os.path.join(share, "From phone", "old.txt"), "old")
        httpd, _ = server.start(share, free_port())
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(os.listdir(os.path.join(share, "From phone")), [])
        self.assertEqual(os.listdir(os.path.join(share, "Earlier sessions")), ["old.txt"])

    def test_each_start_makes_a_fresh_token_pin_and_no_sessions(self):
        httpd, _ = server.start(os.path.join(self.tmp, "share"), free_port())
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        first = server.STATE["token"]
        server.STATE["sessions"].add("old-session")
        httpd2, _ = server.start(os.path.join(self.tmp, "share"), free_port())
        self.addCleanup(httpd2.server_close)
        self.addCleanup(httpd2.shutdown)
        self.assertNotEqual(server.STATE["token"], first)
        self.assertEqual(server.STATE["sessions"], set())

    def test_picks_next_port_when_busy(self):
        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        httpd, got = server.start(os.path.join(self.tmp, "share"), port)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertNotEqual(got, port)


class ResolveTests(TmpDirCase):
    def test_file_in_root_and_in_folder(self):
        self.assertEqual(server.resolve(self.tmp, "a.txt"), os.path.join(self.tmp, "a.txt"))
        self.assertEqual(server.resolve(self.tmp, "a.txt", "Trip"), os.path.join(self.tmp, "Trip", "a.txt"))
        self.assertEqual(server.resolve(self.tmp, None, "Trip"), os.path.join(self.tmp, "Trip"))
        self.assertEqual(server.resolve(self.tmp, "", "Trip"), os.path.join(self.tmp, "Trip"))

    def test_invalid_names_never_fall_back_to_the_root_folder(self):
        for name, folder in ((None, ".."), ("", ".."), (None, None), ("", ""), ("..", None), (None, "."),
                             ("a.txt", ".."), ("..", "Trip"), (None, "/"), ("/", None)):
            self.assertIsNone(server.resolve(self.tmp, name, folder), (name, folder))

    def test_path_tricks_are_reduced_to_a_single_name(self):
        self.assertEqual(server.resolve(self.tmp, "../x.txt"), os.path.join(self.tmp, "x.txt"))
        self.assertEqual(server.resolve(self.tmp, None, "../../Trip"), os.path.join(self.tmp, "Trip"))


class CollectFilesTests(TmpDirCase):
    def make(self, *rel):
        for r in rel:
            p = os.path.join(self.tmp, *r.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            write_text(p)

    def test_finds_files_in_subfolders_and_skips_hidden(self):
        self.make("a.txt", "sub/b.txt", "sub/deep/c.txt", ".hidden", ".git/config", "sub/.DS_Store")
        rel = sorted(os.path.relpath(f, self.tmp).replace(os.sep, "/") for f in server.collect_files(self.tmp))
        self.assertEqual(rel, ["a.txt", "sub/b.txt", "sub/deep/c.txt"])

    def test_empty_folder(self):
        self.assertEqual(server.collect_files(self.tmp), [])


class AddToOutboxTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "dropped")
        shutil.rmtree(self.src, ignore_errors=True)
        for rel in ("Photos/a.jpg", "Photos/2024/b.jpg", "Photos/2024/raw/c.jpg", "Photos/.hidden", "loose.txt"):
            path = os.path.join(self.src, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_text(path, rel)

    def test_dropped_folder_includes_subfolder_files(self):
        server.add_to_outbox([os.path.join(self.src, "Photos")])
        self.assertEqual(sorted(os.listdir(os.path.join(self.outbox, "Photos"))), ["a.jpg", "b.jpg", "c.jpg"])

    def test_typed_folder_name_wins_over_dropped_folder_name(self):
        server.add_to_outbox([os.path.join(self.src, "Photos")], "Holiday")
        self.assertEqual(len(os.listdir(os.path.join(self.outbox, "Holiday"))), 3)
        self.assertFalse(os.path.exists(os.path.join(self.outbox, "Photos")))

    def test_loose_file_goes_to_box_or_named_folder(self):
        server.add_to_outbox([os.path.join(self.src, "loose.txt")])
        server.add_to_outbox([os.path.join(self.src, "loose.txt")], "Docs")
        self.assertEqual(os.listdir(self.outbox).count("loose.txt"), 1)
        self.assertEqual(os.listdir(os.path.join(self.outbox, "Docs")), ["loose.txt"])

    def test_same_file_names_in_different_subfolders_do_not_overwrite(self):
        for sub in ("one", "two"):
            os.makedirs(os.path.join(self.src, "Dup", sub))
            write_text(os.path.join(self.src, "Dup", sub, "same.txt"), sub)
        server.add_to_outbox([os.path.join(self.src, "Dup")])
        names = sorted(os.listdir(os.path.join(self.outbox, "Dup")))
        self.assertEqual(names, ["same (1).txt", "same.txt"])
        self.assertEqual(sorted(read_bytes(os.path.join(self.outbox, "Dup", n)) for n in names), [b"one", b"two"])


class ParseRangeTests(unittest.TestCase):
    def test_no_or_unusable_header_means_whole_file(self):
        for h in (None, "", "items=0-5", "bytes=abc-def", "bytes=5-2", "bytes=-", "bytes=0-1,5-6", "bytes=-x"):
            self.assertIsNone(server.parse_range(h, 100), h)

    def test_explicit_open_ended_and_suffix_ranges(self):
        self.assertEqual(server.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(server.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(server.parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(server.parse_range("bytes=-500", 100), (0, 99))

    def test_end_past_the_file_is_clamped(self):
        self.assertEqual(server.parse_range("bytes=50-5000", 100), (50, 99))

    def test_start_past_the_file_is_unsatisfiable(self):
        self.assertEqual(server.parse_range("bytes=100-", 100), "unsatisfiable")
        self.assertEqual(server.parse_range("bytes=-0", 100), "unsatisfiable")

    def test_empty_file_ignores_ranges(self):
        self.assertIsNone(server.parse_range("bytes=0-5", 0))


class RangeDownloadTests(ServerCase):
    DATA = bytes(range(256)) * 40  # 10240 bytes, every offset has a distinct-ish value

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.outbox, "big.bin"), "wb") as f:
            f.write(self.DATA)
        self.auth = self.pair()

    def get(self, rng=None):
        h = dict(self.auth)
        if rng:
            h["Range"] = rng
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/files/big.bin", headers={"Host": f"127.0.0.1:{self.port}", **h})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_full_download_advertises_range_support(self):
        r, body = self.get()
        self.assertEqual((r.status, body), (200, self.DATA))
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(r.getheader("Content-Length"), str(len(self.DATA)))

    def test_partial_content(self):
        r, body = self.get("bytes=100-199")
        self.assertEqual(r.status, 206)
        self.assertEqual(body, self.DATA[100:200])
        self.assertEqual(r.getheader("Content-Range"), f"bytes 100-199/{len(self.DATA)}")
        self.assertEqual(r.getheader("Content-Length"), "100")

    def test_open_ended_and_suffix(self):
        self.assertEqual(self.get("bytes=10000-")[1], self.DATA[10000:])
        self.assertEqual(self.get("bytes=-240")[1], self.DATA[-240:])

    def test_resuming_an_interrupted_download_gives_the_identical_file(self):
        first = self.get("bytes=0-4999")[1]  # "connection dropped" after 5000 bytes
        rest = self.get(f"bytes={len(first)}-")[1]
        self.assertEqual(first + rest, self.DATA)

    def test_unsatisfiable_range_is_416(self):
        r, _ = self.get(f"bytes={len(self.DATA)}-")
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Content-Range"), f"bytes */{len(self.DATA)}")

    def test_garbage_range_is_ignored(self):
        r, body = self.get("bytes=abc")
        self.assertEqual((r.status, body), (200, self.DATA))

    def test_empty_file_downloads_cleanly(self):
        open(os.path.join(self.outbox, "empty.txt"), "wb").close()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/files/empty.txt", headers={"Host": f"127.0.0.1:{self.port}", "Range": "bytes=0-5", **self.auth})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b""))
        c.close()

    def test_range_still_needs_login(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/files/big.bin", headers={"Host": f"127.0.0.1:{self.port}", "Range": "bytes=0-9"})
        self.assertEqual(c.getresponse().status, 401)
        c.close()


class StreamingZipTests(ServerCase):
    def test_large_zip_is_streamed_and_valid(self):
        import io
        import zipfile
        os.makedirs(os.path.join(self.outbox, "Big"))
        payload = {f"f{i}.bin": os.urandom(700_000) for i in range(4)}
        for n, d in payload.items():
            with open(os.path.join(self.outbox, "Big", n), "wb") as f:
                f.write(d)
        auth = self.pair()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", "/zip/Big", headers={"Host": f"127.0.0.1:{self.port}", **auth})
        r = c.getresponse()
        body = r.read()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/zip")
        self.assertIsNone(r.getheader("Content-Length"))  # streamed, not built in advance
        self.assertIn("Big.zip", r.getheader("Content-Disposition"))
        z = zipfile.ZipFile(io.BytesIO(body))
        self.assertIsNone(z.testzip())
        self.assertEqual(sorted(z.namelist()), [f"Big/{n}" for n in sorted(payload)])
        for n, d in payload.items():
            self.assertEqual(z.read(f"Big/{n}"), d)

    def test_zip_does_not_need_a_temporary_file(self):
        os.makedirs(os.path.join(self.outbox, "T"))
        write_text(os.path.join(self.outbox, "T", "a.txt"), "hello")
        auth = self.pair()
        with mock.patch("tempfile.TemporaryFile", side_effect=AssertionError("temp file used")):
            st, _, body = self.req("GET", "/zip/T", auth)
        self.assertEqual(st, 200)
        self.assertGreater(len(body), 20)

    def test_client_hanging_up_mid_zip_does_not_break_the_server(self):
        os.makedirs(os.path.join(self.outbox, "Cut"))
        with open(os.path.join(self.outbox, "Cut", "big.bin"), "wb") as f:
            f.write(os.urandom(8_000_000))
        auth = self.pair()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall((f"GET /zip/Cut HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nCookie: {auth['Cookie']}\r\n\r\n").encode())
            s.recv(1000)
        self.assertEqual(self.req("GET", "/desktop/info")[0], 200)


class QrComputedOnceTests(ServerCase):
    def test_info_polling_does_not_recompute_the_qr_code(self):
        with mock.patch.object(server, "qr_matrix", side_effect=AssertionError("recomputed")):
            for _ in range(3):
                info = json.loads(self.req("GET", "/desktop/info")[2])
        self.assertEqual(info["qr"], server.STATE["qr"])
        self.assertEqual(info["qr"], server.qr_matrix(server.STATE["url"]))


class ConcurrentPairingTests(ServerCase):
    def test_parallel_wrong_guesses_are_counted_exactly(self):
        server.new_pin()
        codes, lock = [], threading.Lock()

        # 5 characters can never equal a 4-digit PIN, whatever the PIN becomes after each lockout.
        def guess5():
            c = self.req("POST", "/pair", {"Cookie": f"q={self.token}"}, "99999")[0]
            with lock:
                codes.append(c)

        threads = [threading.Thread(target=guess5) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(codes.count(429), 4)  # one lockout per 5 failures, none lost
        self.assertEqual(codes.count(403), 16)
        self.assertEqual(server.STATE["fails"], 0)


class OutboxClearedOnStartTests(TmpDirCase):
    def test_leftover_copies_are_deleted_not_archived(self):
        share = os.path.join(self.tmp, "share")
        os.makedirs(os.path.join(share, "For phone", "Trip"))
        write_text(os.path.join(share, "For phone", "Trip", "a.jpg"))
        write_text(os.path.join(share, "For phone", "loose.txt"))
        httpd, _ = server.start(share, free_port())
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(os.listdir(os.path.join(share, "For phone")), [])
        self.assertFalse(os.path.exists(os.path.join(share, "Earlier sessions")))

    def test_unsaved_received_files_are_still_archived(self):
        share = os.path.join(self.tmp, "share")
        os.makedirs(os.path.join(share, "From phone", "Trip"))
        write_text(os.path.join(share, "From phone", "Trip", "a.jpg"))
        httpd, _ = server.start(share, free_port())
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(os.listdir(os.path.join(share, "From phone")), [])
        self.assertEqual(os.listdir(os.path.join(share, "Earlier sessions", "Trip")), ["a.jpg"])

    def test_hidden_files_in_the_outbox_are_left_alone(self):
        share = os.path.join(self.tmp, "share")
        os.makedirs(os.path.join(share, "For phone"))
        write_text(os.path.join(share, "For phone", ".keep"))
        httpd, _ = server.start(share, free_port())
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(os.listdir(os.path.join(share, "For phone")), [".keep"])


if __name__ == "__main__":
    unittest.main()
