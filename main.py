#!/usr/bin/env python3
"""
p2p_story_client.py (updated)

Client for lab machines that:
- Does peer discovery + auto verify/connect (console)
- Registers with projector server and sends heartbeats
- Pushes game updates to projector server (server is authoritative display)
- Uses server /pass endpoint for passing turns (server picks a random valid participant if not specified)

Usage:
    pip install requests
    python p2p_story_client.py http://<server-ip>:5001
"""
import socket, threading, json, uuid, time, random, sys
import requests

# ---- Config ----
if len(sys.argv) >= 2:
    SERVER_URL = sys.argv[1].rstrip("/")
else:
    SERVER_URL = "http://localhost:5001"
print("Projector server URL:", SERVER_URL)

DISCOVERY_PORT = 37020
DISCOVERY_INTERVAL = 1.0
DISCOVERY_TIMEOUT = 8.0
BUFFER_SIZE = 65536
ENCODING = "utf-8"
HEARTBEAT_INTERVAL = 12.0  # seconds

def now_ts():
    return time.time()

def norm(s): return str(s).strip().lower()
def list_contains(list_or_str, query):
    if isinstance(list_or_str, str): return norm(list_or_str) == norm(query)
    for item in list_or_str:
        if norm(item) == norm(query): return True
    return False

class ClientNode:
    def __init__(self, profile):
        self.id = str(uuid.uuid4())
        self.profile = profile
        self.peers = {}
        self.connections = {}
        self.tcp_port = self._pick_port()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        # local copy of game_state (server is authoritative)
        self.game_state = {"game_id": str(uuid.uuid4()), "seq": 0, "sentence": "", "holder_id": None, "completed": True}

    def _pick_port(self):
        s = socket.socket(); s.bind(("",0)); p = s.getsockname()[1]; s.close(); return p

    # Discovery
    def start_broadcaster(self):
        def bcast():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            msg = json.dumps({"type":"hello","id":self.id,"name":self.profile.get("name"),"tcp_port": self.tcp_port}).encode(ENCODING)
            while not self.stop_event.is_set():
                try:
                    sock.sendto(msg, ("<broadcast>", DISCOVERY_PORT))
                except Exception:
                    try: sock.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
                    except: pass
                time.sleep(DISCOVERY_INTERVAL)
            sock.close()
        t = threading.Thread(target=bcast, daemon=True); t.start(); return t

    def start_listener(self):
        def listen():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: sock.bind(("", DISCOVERY_PORT))
            except Exception: sock.bind(("0.0.0.0", DISCOVERY_PORT))
            while not self.stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(BUFFER_SIZE)
                    obj = json.loads(data.decode(ENCODING))
                    if obj.get("type") != "hello": continue
                    pid = obj.get("id")
                    if pid == self.id: continue
                    with self.lock:
                        self.peers[pid] = {"id": pid, "name": obj.get("name","?"), "host": addr[0], "port": int(obj.get("tcp_port")), "last_seen": now_ts()}
                except Exception:
                    pass
            sock.close()
        t = threading.Thread(target=listen, daemon=True); t.start(); return t

    def start_pruner(self):
        def pruner():
            while not self.stop_event.is_set():
                with self.lock:
                    remove = []
                    for pid, p in list(self.peers.items()):
                        if now_ts() - p["last_seen"] > DISCOVERY_TIMEOUT:
                            remove.append(pid)
                    for pid in remove:
                        del self.peers[pid]
                time.sleep(2.0)
        t = threading.Thread(target=pruner, daemon=True); t.start(); return t

    def start_tcp_server(self):
        def handle_client(conn, addr):
            f = conn.makefile(mode="rw", encoding=ENCODING)
            try:
                raw = f.readline()
                if not raw: return
                msg = json.loads(raw)
                typ = msg.get("type")
                if typ == "verify":
                    field = msg.get("field"); value = msg.get("value")
                    actual = self.profile.get(field)
                    match = False
                    if isinstance(actual, list):
                        match = list_contains(actual, value)
                    else:
                        match = norm(actual) == norm(value)
                    f.write(json.dumps({"type":"verify_result","ok": bool(match)}) + "\n"); f.flush()
                    if match:
                        raw2 = f.readline()
                        if raw2:
                            msg2 = json.loads(raw2)
                            if msg2.get("type") == "connect":
                                initiator_profile = msg2.get("profile")
                                with self.lock:
                                    self.connections[initiator_profile.get("id")] = initiator_profile
                                f.write(json.dumps({"type":"connect_ack","profile": self.full_profile()}) + "\n"); f.flush()
                elif typ == "connect":
                    incoming = msg.get("profile")
                    with self.lock:
                        self.connections[incoming.get("id")] = incoming
                    f.write(json.dumps({"type":"connect_ack","profile": self.full_profile()}) + "\n"); f.flush()
            except Exception:
                pass
            finally:
                try: f.close()
                except: pass
                try: conn.close()
                except: pass

        def server_loop():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.tcp_port)); s.listen(6)
            while not self.stop_event.is_set():
                try:
                    s.settimeout(1.0)
                    conn, addr = s.accept()
                    t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True); t.start()
                except socket.timeout:
                    continue
                except Exception:
                    break
            s.close()
        t = threading.Thread(target=server_loop, daemon=True); t.start(); return t

    def full_profile(self):
        p = dict(self.profile); p["id"] = self.id; return p

    # connection attempt
    def attempt_random_connection(self):
        with self.lock:
            candidates = [p for p in self.peers.values() if p["id"] not in self.connections]
        if not candidates:
            print("No undiscovered peers.")
            return
        peer = random.choice(candidates)
        fields = ["name","hobbies","where_from","pets","classes"]
        field = random.choice(fields)
        print(f"Ask {peer['name']} about '{field}' and type their answer:")
        value = input("Value: ").strip()
        if not value:
            print("aborting")
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((peer["host"], peer["port"]))
            f = s.makefile(mode="rw", encoding=ENCODING)
            f.write(json.dumps({"type":"verify","from_id":self.id,"from_name":self.profile.get("name"),"field":field,"value":value}) + "\n"); f.flush()
            raw = f.readline()
            if raw:
                resp = json.loads(raw); ok = resp.get("ok",False)
                if ok:
                    f.write(json.dumps({"type":"connect","profile": self.full_profile()}) + "\n"); f.flush()
                    raw2 = f.readline()
                    if raw2:
                        ack = json.loads(raw2); their = ack.get("profile")
                        with self.lock:
                            self.connections[their.get("id")] = their
                        print("Connected to", their.get("name"))
                    else:
                        print("Verified but no profile received")
                else:
                    print("verification failed")
            else:
                print("no reply")
            try: f.close()
            except: pass
            try: s.close()
            except: pass
        except Exception as e:
            print("error connecting:", e)

    # ----- server integration -----
    def register_with_server(self):
        url = SERVER_URL + "/register"
        payload = {"id": self.id, "name": self.profile.get("name"), "profile": self.profile}
        try:
            requests.post(url, json=payload, timeout=2.0)
            print("Registered with server:", SERVER_URL)
        except Exception as e:
            print("Register error:", e)

    def heartbeat_loop(self):
        def hb():
            url = SERVER_URL + "/heartbeat"
            while not self.stop_event.is_set():
                try:
                    requests.post(url, json={"id": self.id}, timeout=2.0)
                except Exception:
                    pass
                time.sleep(HEARTBEAT_INTERVAL)
        t = threading.Thread(target=hb, daemon=True); t.start(); return t

    def push_game_state_to_server(self):
        url = SERVER_URL + "/update_game"
        payload = {"id": self.id, "game_state": self.game_state}
        try:
            r = requests.post(url, json=payload, timeout=2.0)
            if r.status_code != 200:
                print("Server rejected state:", r.text)
        except Exception as e:
            print("Push error:", e)

    def sync_state_from_server(self):
        try:
            r = requests.get(SERVER_URL + "/state", timeout=2.0)
            j = r.json()
            gs = j.get("game_state")
            if gs:
                with self.lock:
                    self.game_state = gs
        except Exception:
            pass

    # game actions (client modifies local state then pushes)
    def start_sentence(self, word):
        word = word.strip()
        if not word:
            print("Empty.")
            return
        with self.lock:
            self.game_state["game_id"] = str(uuid.uuid4())
            self.game_state["seq"] += 1
            self.game_state["sentence"] = word
            self.game_state["holder_id"] = self.id
            self.game_state["completed"] = False
        self.push_game_state_to_server()
        print("Started sentence:", self.game_state["sentence"])

    def add_word(self, word):
        self.sync_state_from_server()
        with self.lock:
            if self.game_state.get("holder_id") != self.id:
                print("Not your turn.")
                return
            if self.game_state.get("completed"):
                print("Sentence completed.")
                return
            w = word.strip()
            if not w:
                print("Empty.")
                return
            if self.game_state["sentence"]:
                self.game_state["sentence"] += " " + w
            else:
                self.game_state["sentence"] = w
            self.game_state["seq"] += 1
        self.push_game_state_to_server()
        print("Added word. Sentence now:", self.game_state["sentence"])

    def end_sentence(self):
        with self.lock:
            if self.game_state.get("holder_id") != self.id:
                print("Not your turn.")
                return
            if self.game_state.get("completed"):
                print("Already completed.")
                return
            s = self.game_state["sentence"].strip()
            if not s.endswith("."):
                s = s + "."
            self.game_state["sentence"] = s
            self.game_state["completed"] = True
            self.game_state["seq"] += 1
        self.push_game_state_to_server()
        print("Ended sentence:", self.game_state["sentence"])

    def pass_turn(self, target_id=None):
        url = SERVER_URL + "/pass"
        payload = {"id": self.id}
        if target_id: payload["target_id"] = target_id
        try:
            r = requests.post(url, json=payload, timeout=3.0)
            if r.status_code == 200:
                j = r.json()
                new_holder = j.get("new_holder")
                print("Pass accepted. New holder:", new_holder)
                # sync authoritative state
                self.sync_state_from_server()
            else:
                print("Pass failed:", r.text)
        except Exception as e:
            print("Pass error:", e)

    def show_peers(self):
        with self.lock:
            ps = list(self.peers.values())
        if not ps:
            print("(no peers)")
            return
        for p in ps:
            print(f"- {p['name']} @ {p['host']}:{p['port']} id={p['id'][:6]} last={now_ts()-p['last_seen']:.1f}s")

    def show_connections(self):
        with self.lock:
            cs = list(self.connections.values())
        if not cs:
            print("(no connections)")
            return
        for c in cs:
            print(f"- {c['name']} ({c.get('where_from','')}) id={c['id'][:6]}")

    def stop(self):
        self.stop_event.set()

# ---- main loop ----
def main():
    print("=== P2P Story Client (updated) ===")
    name = input("Your name: ").strip()
    while not name:
        name = input("Please enter a name: ").strip()
    hobbies = input("Hobbies (comma-separated): ").strip()
    where_from = input("Where are you from?: ").strip()
    pets = input("Pets (comma-separated): ").strip()
    classes = input("CS classes this semester (comma-separated): ").strip()

    profile = {
        "name": name,
        "hobbies": [h.strip() for h in hobbies.split(",")] if hobbies else [],
        "where_from": where_from,
        "pets": [p.strip() for p in pets.split(",")] if pets else [],
        "classes": [c.strip() for c in classes.split(",")] if classes else []
    }

    node = ClientNode(profile)
    print("Node id:", node.id[:8], "tcp_port:", node.tcp_port)
    node.start_broadcaster()
    node.start_listener()
    node.start_pruner()
    node.start_tcp_server()

    # Register and start heartbeat
    node.register_with_server()
    node.heartbeat_loop()
    node.sync_state_from_server()

    try:
        while True:
            print("\nMenu: [d]iscovered peers  [c]connect (random verify)  [p]print connections")
            print("      [s]tart sentence  [a]dd word  [e]nd sentence  [x]pass turn  [q]uit")
            cmd = input("> ").strip().lower()
            if cmd == "d":
                node.show_peers()
            elif cmd == "c":
                node.attempt_random_connection()
            elif cmd == "p":
                node.show_connections()
            elif cmd == "s":
                w = input("Start with word: ").strip()
                node.start_sentence(w)
            elif cmd == "a":
                w = input("Word to add: ").strip()
                node.add_word(w)
            elif cmd == "e":
                node.end_sentence()
            elif cmd == "x":
                target = input("Target id to pass to (enter to let server pick randomly): ").strip()
                node.pass_turn(target_id=target if target else None)
            elif cmd == "q":
                print("Quitting...")
                break
            else:
                print("Unknown command.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        node.stop()
        time.sleep(0.2)
        sys.exit(0)

if __name__ == "__main__":
    random.seed()
    main()
