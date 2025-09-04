#!/usr/bin/env python3
"""
p2p_story_server_readonly.py (updated)

Read-only projector server for One-Word Story.

Fixes:
- pass endpoint now reliably picks a random *other* participant (active first, else any other).
- server tracks participant last_seen and offers /heartbeat for clients to keep themselves active.
- safer checks and clearer responses.

Run:
    pip install flask
    python p2p_story_server_readonly.py
"""
from flask import Flask, request, jsonify, render_template_string, Response
import threading, queue, time, json, random, uuid

app = Flask(__name__)

# In-memory participant store
participants = {}   # id -> {"id","name","profile","last_seen"}
participants_lock = threading.Lock()

# Authoritative game state
game_state = {
    "game_id": str(uuid.uuid4()),
    "seq": 0,
    "sentence": "",
    "holder_id": None,
    "completed": True
}
game_lock = threading.Lock()

# SSE queues
sse_queues = []
sse_lock = threading.Lock()

# Consider participants "active" if they've pinged server in this many seconds
ACTIVE_TIMEOUT = 90.0  # seconds

HTML = """<!doctype html>
<html><head><meta charset="utf-8"/><title>One-Word Story — Projector (Read-only)</title>
<style>
body{font-family:system-ui, -apple-system, "Segoe UI", Roboto, Arial;margin:24px;background:#071029;color:#fff}
.card{padding:22px;border-radius:12px;background:linear-gradient(135deg,#0f1724,#071029);max-width:1200px}
.sentence{font-size:46px;margin-bottom:12px;line-height:1.2}
.meta{color:#9aa6b2;margin-bottom:8px}
.participants{margin-top:14px;color:#cbd5e1}
code{background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:6px}
</style></head><body>
<div class="card">
  <h1>One-Word Story</h1>
  <div class="sentence" id="sentence">(no active sentence)</div>
  <div class="meta" id="holder">Holder: (none)</div>
  <div class="participants"><strong>Participants:</strong> <span id="parts">(none)</span></div>
</div>

<script>
const evt = new EventSource('/stream');
evt.onmessage = function(e){
  try {
    const gs = JSON.parse(e.data);
    document.getElementById('sentence').innerText = gs.sentence || "(no active sentence)";
    const holder = gs.holder_id ? gs.holder_id.substring(0,8) : "(none)";
    document.getElementById('holder').innerText = "Holder: " + holder + (gs.completed ? " (completed)" : "");
    fetch('/state').then(r=>r.json()).then(data=>{
      const parts = data.participants || [];
      if (parts.length === 0) {
        document.getElementById('parts').innerText = "(none)";
      } else {
        document.getElementById('parts').innerText = parts.map(p => p.name + " ("+p.id.substring(0,8)+")").join(" • ");
      }
    });
  } catch (err) { console.warn(err); }
};
</script>
</body></html>
"""

# SSE helpers
def register_sse():
    q = queue.Queue()
    with sse_lock:
        sse_queues.append(q)
    q.put(game_state.copy())
    return q

def unregister_sse(q):
    with sse_lock:
        try: sse_queues.remove(q)
        except ValueError: pass

def broadcast_state():
    snap = None
    with game_lock:
        snap = game_state.copy()
    with sse_lock:
        for q in list(sse_queues):
            try:
                q.put(snap)
            except Exception:
                pass

# Helpers
def now_ts():
    return time.time()

# Routes
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/stream")
def stream():
    q = register_sse()
    def gen():
        try:
            while True:
                try:
                    gs = q.get(timeout=30.0)
                except queue.Empty:
                    yield ":\n\n"
                    continue
                yield f"data: {json.dumps(gs)}\n\n"
        finally:
            unregister_sse(q)
    return Response(gen(), mimetype="text/event-stream")

@app.route("/state")
def state():
    with participants_lock:
        parts = list(participants.values())
    with game_lock:
        gs = game_state.copy()
    return jsonify({"participants": parts, "game_state": gs})

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    pid = data.get("id")
    if not pid:
        return jsonify({"ok":False, "error":"id required"}), 400
    name = data.get("name") or ("user-" + pid[:8])
    profile = data.get("profile", {})
    with participants_lock:
        participants[pid] = {"id": pid, "name": name, "profile": profile, "last_seen": now_ts()}
    broadcast_state()
    return jsonify({"ok":True})

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    data = request.get_json(force=True)
    pid = data.get("id")
    if not pid:
        return jsonify({"ok":False, "error":"id required"}), 400
    with participants_lock:
        if pid in participants:
            participants[pid]["last_seen"] = now_ts()
            return jsonify({"ok":True})
        else:
            return jsonify({"ok":False, "error":"unknown id"}), 404

@app.route("/update_game", methods=["POST"])
def update_game():
    data = request.get_json(force=True)
    pid = data.get("id")
    gs = data.get("game_state")
    if not pid or not gs:
        return jsonify({"ok":False, "error":"id and game_state required"}), 400
    # Only accept if the sender is the holder in posted state
    if gs.get("holder_id") != pid:
        return jsonify({"ok":False, "error":"sender must be the holder"}, 403)
    with game_lock:
        # Accept posted state
        # Simple: replace authoritative copy with posted one (trust clients lightly)
        game_state.update(gs)
    broadcast_state()
    return jsonify({"ok":True})

@app.route("/pass", methods=["POST"])
def do_pass():
    """
    Body: {"id": requester_id, "target_id": optional}
    Server chooses a random *other* participant (active first).
    """
    data = request.get_json(force=True)
    pid = data.get("id")
    target = data.get("target_id")
    if not pid:
        return jsonify({"ok":False, "error":"id required"}), 400

    with participants_lock:
        # Build candidate lists (exclude requester)
        other_parts = [p for p in participants.values() if p["id"] != pid]
        if not other_parts:
            return jsonify({"ok":False, "error":"no other participants registered"}, 400)
        now = now_ts()
        # active participants: those seen recently
        active = [p for p in other_parts if (now - p.get("last_seen",0)) <= ACTIVE_TIMEOUT]

    chosen = None
    if target:
        # if target provided, check it exists and is not the requester
        with participants_lock:
            if target in participants and target != pid:
                chosen = target
            else:
                return jsonify({"ok":False, "error":"requested target invalid or not found"}), 400
    else:
        # pick from active first
        pool = active if active else other_parts
        print(pool, active, other_parts)

        # random.choice requires non-empty list
        chosen = random.choice(pool)["id"]

    with game_lock:
        game_state["holder_id"] = chosen
        game_state["seq"] = game_state.get("seq", 0) + 1

    broadcast_state()
    return jsonify({"ok":True, "new_holder": chosen})

# Optional: prune stale participants endpoint (admin)
@app.route("/prune_stale", methods=["POST"])
def prune_stale():
    cutoff = float(request.args.get("cutoff", 300.0))
    now = now_ts()
    removed = []
    with participants_lock:
        for pid in list(participants.keys()):
            if now - participants[pid].get("last_seen",0) > cutoff:
                removed.append(pid)
                del participants[pid]
    if removed:
        broadcast_state()
    return jsonify({"ok":True, "removed": removed})

if __name__ == "__main__":
    print("Starting projector server on http://0.0.0.0:5001 (read-only UI)")
    random.seed()  # ensure randomness
    app.run(host="0.0.0.0", port=5001, threaded=True)
