import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from app import Adapter, Handler, Prototype, Store


class PrototypeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.jsonl"
        self.store = Store(self.path)
        self.app = Prototype(self.store, Adapter(delay=0.01))

    def tearDown(self):
        self.temp.cleanup()

    def wait_for(self, branch_id):
        for _ in range(100):
            branch = next(b for b in self.store.snapshot()["branches"] if b["id"] == branch_id)
            if branch["status"] == "stopped" and branch["messages"][-1]["role"] == "assistant":
                return branch
            time.sleep(0.01)
        self.fail("agent did not finish")

    def test_acceptance_demo_and_restart(self):
        root = self.store.create("Root")
        self.app.send(root["id"], "Choose a colour")
        root = self.wait_for(root["id"])
        fork_point = root["messages"][0]["id"]

        red = self.store.fork(root["id"], fork_point, "Red")
        blue = self.store.fork(root["id"], fork_point, "Blue")
        self.app.send(red["id"], "Use red")
        self.app.send(blue["id"], "Use blue")
        self.wait_for(red["id"])
        self.wait_for(blue["id"])

        self.app.send(red["id"], "This reply will be cancelled")
        self.app.send(red["id"], "Redirect to crimson", redirect=True)
        self.store.status(blue["id"], "stopped")
        red_done = self.wait_for(red["id"])
        self.assertIn("crimson", red_done["messages"][-1]["text"])

        comparison = self.store.compare([red["id"], blue["id"]])
        self.assertEqual(["Red", "Blue"], [row["name"] for row in comparison])
        self.assertNotEqual(comparison[0]["output"], comparison[1]["output"])

        restored = Store(self.path).snapshot()
        self.assertEqual(3, len(restored["branches"]))
        restored_red = next(b for b in restored["branches"] if b["name"] == "Red")
        self.assertEqual(root["id"], restored_red["parent_id"])
        self.assertIn("crimson", restored_red["messages"][-1]["text"])

    def test_fork_stops_at_selected_message(self):
        root = self.store.create("Root")
        first = self.store.add_message(root["id"], "user", "first")
        self.store.add_message(root["id"], "assistant", "second")
        child = self.store.fork(root["id"], first["id"], "Child")
        self.assertEqual(["first"], [message["text"] for message in child["messages"]])

    def test_stop_discards_in_flight_output(self):
        root = self.store.create("Root")
        self.app = Prototype(self.store, Adapter(delay=0.05))
        self.app.send(root["id"], "slow")
        self.store.status(root["id"], "stopped")
        time.sleep(0.08)
        branch = self.store.snapshot()["branches"][0]
        self.assertEqual(["user"], [message["role"] for message in branch["messages"]])

    def test_http_surface_serves_ui_and_actions(self):
        Handler.prototype = self.app
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(path, body):
            request = Request(
                base + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            return json.load(urlopen(request))

        try:
            self.assertIn(b"<title>Agent Forks</title>", urlopen(base + "/").read())
            branch = post("/api/branches", {"name": "HTTP agent"})
            post(f"/api/branches/{branch['id']}/messages", {"text": "hello"})
            finished = self.wait_for(branch["id"])
            comparison = post("/api/compare", {"branch_ids": [branch["id"]]})
            self.assertEqual(finished["messages"][-1]["text"], comparison[0]["output"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
