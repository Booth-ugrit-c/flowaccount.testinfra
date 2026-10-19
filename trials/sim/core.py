import heapq
import json

CAUSES = ["startup", "step_barrier", "partition_boundary", "account_cap", "e2e_prereq",
          "fragmentation", "sched_latency", "tail", "other"]


def m(x):
    return int(round(x * 1000))


class UnitWL:
    def __init__(self, build=(), affected=(), ui=()):
        self.build, self.affected, self.ui = list(build), list(affected), list(ui)

    def all_tasks(self):
        return self.build + self.affected + self.ui


class E2EWL:
    def __init__(self, build_s, startup_s, tests):
        self.build_s, self.startup_s, self.tests = build_s, startup_s, list(tests)


class Gate:
    def __init__(self, arrival, unit, e2e):
        self.arrival, self.unit, self.e2e = arrival, unit, e2e


_cache = {}


def _load(path):
    key = str(path)
    if key not in _cache:
        with open(path, encoding="utf-8") as f:
            _cache[key] = json.load(f)
    return _cache[key]


def _run(path, run_id):
    return next(x for x in _load(path)["runs"] if str(x["run_id"]) == str(run_id))


def load_unit(path, run_id, build_lanes=1):
    r = _run(path, run_id)

    def times(step):
        return [p["test_s"] for p in r["projects"]
                if p["step"] == step and not p["cache_hit"] and not p.get("no_tests") and p["test_s"] is not None]
    b = r["build_s"] or 0
    return UnitWL(build=[b] * build_lanes if b > 0 else [], affected=times("affected"), ui=times("ui"))


def load_e2e(path, run_id):
    r = _run(path, run_id)
    tests = sorted(r["tests"], key=lambda t: (t["start_offset_s"], t["id"]))
    return E2EWL(r["build_s"], r["startup_s"], [t["dur_s"] for t in tests])


def run_ids(path):
    return [str(r["run_id"]) for r in _load(path)["runs"]]


class Eng:
    def __init__(self):
        self.t = 0.0
        self.q = []
        self.n = 0

    def at(self, t, fn, *a):
        heapq.heappush(self.q, (t, self.n, fn, a))
        self.n += 1

    def run(self, observe):
        while self.q:
            t = self.q[0][0]
            self.t = t
            while self.q and self.q[0][0] <= t + 1e-9:
                _, _, fn, a = heapq.heappop(self.q)
                fn(*a)
            observe(t)


def run_lanes(eng, tasks, conc, on_start, on_end, on_done):
    st = {"i": 0, "run": 0}

    def launch():
        while st["run"] < conc and st["i"] < len(tasks):
            task = tasks[st["i"]]
            st["i"] += 1
            st["run"] += 1
            on_start(task)
            eng.at(eng.t + task[0], finish, task)

    def finish(task):
        st["run"] -= 1
        on_end(task)
        launch()
        if st["run"] == 0 and st["i"] >= len(tasks):
            on_done()

    if not tasks:
        on_done()
    else:
        launch()
    return st


class GS:
    def __init__(self, gate, idx):
        self.gate, self.idx = gate, idx
        self.arrived = False
        self.u_unstarted = 0
        self.e_unstarted = 0
        self.u_run = 0
        self.e_run = 0
        self.u_phase = "queued"
        self.e_phase = "queued"
        self.unit_first_start = None
        self.unit_end = None
        self.e2e_first_test_start = None
        self.e2e_end = None
        self.end = None
        self.lane = None
        self.n_workers = 0


class Recorder:
    def __init__(self):
        self.rows = []

    def add(self, t, busy_u, busy_e, causes, unstarted, held):
        self.rows.append((t, busy_u, busy_e, causes, unstarted, held))


def _check_done(gs, eng):
    if gs.end is None and gs.u_phase == "done" and gs.e_phase == "done":
        gs.end = eng.t


class PartSim:
    def __init__(self, arm, gates, w, accounts, unit_cpu, pool, pod_overhead, a_conc):
        self.arm, self.w, self.accounts = arm, m(w), accounts
        self.Cu, self.Ce = m(unit_cpu), m(pool) - m(unit_cpu)
        self.pool = m(pool)
        self.oh_u, self.oh_e = pod_overhead if isinstance(pod_overhead, (tuple, list)) else (pod_overhead, pod_overhead)
        self.a_conc = a_conc
        self.eng = Eng()
        self.gs = [GS(g, i) for i, g in enumerate(gates)]
        self.rec = Recorder()
        self.u_owner = None
        self.e_owner = None
        self.u_wait, self.e_wait = [], []
        for s in self.gs:
            self.eng.at(s.gate.arrival, self.arrive, s)

    def arrive(self, s):
        s.arrived = True
        s.u_unstarted = len(s.gate.unit.all_tasks())
        e = s.gate.e2e
        s.e_unstarted = (len(e.tests) + (1 if e.build_s > 0 else 0)) if e else 0
        self.u_wait.append(s)
        if e is None or s.e_unstarted == 0:
            s.e_phase = "done"
        else:
            self.e_wait.append(s)
        self.dispatch()

    def dispatch(self):
        if self.u_owner is None and self.u_wait:
            s = self.u_wait.pop(0)
            self.u_owner = s
            s.u_phase = "overhead"
            self.eng.at(self.eng.t + self.oh_u, self.unit_go, s)
        if self.e_owner is None and self.e_wait and self.Ce > 0:
            s = self.e_wait.pop(0)
            self.e_owner = s
            s.e_phase = "overhead"
            self.eng.at(self.eng.t + self.oh_e, self.e2e_go, s)

    def unit_task_start(self, s, task):
        s.u_unstarted -= 1
        s.u_run += task[1]
        if s.unit_first_start is None:
            s.unit_first_start = self.eng.t

    def unit_task_end(self, s, task):
        s.u_run -= task[1]
        s.unit_end = self.eng.t

    def unit_go(self, s):
        s.u_phase = "run"
        wl = s.gate.unit
        lanes_max = self.Cu // 1000
        if self.arm == "A":
            steps = [([(d, 1000) for d in wl.build], min(self.a_conc[0], lanes_max)),
                     ([(d, 1000) for d in wl.affected], min(self.a_conc[1], lanes_max)),
                     ([(d, 1000) for d in wl.ui], min(self.a_conc[2], lanes_max))]
        else:
            allt = sorted(wl.all_tasks(), key=lambda d: -d)
            steps = [([(d, 1000) for d in allt], lanes_max)]
        self.run_steps(s, steps, 0)

    def run_steps(self, s, steps, i):
        while i < len(steps) and not steps[i][0]:
            i += 1
        if i >= len(steps):
            s.u_phase = "done"
            self.u_owner = None
            _check_done(s, self.eng)
            self.dispatch()
            return
        tasks, conc = steps[i]
        run_lanes(self.eng, tasks, conc, lambda t: self.unit_task_start(s, t), lambda t: self.unit_task_end(s, t),
                  lambda: self.run_steps(s, steps, i + 1))

    def e2e_go(self, s):
        e = s.gate.e2e
        if e.build_s > 0:
            s.e_phase = "build"
            s.e_unstarted -= 1
            s.e_run = 1000
            self.eng.at(self.eng.t + e.build_s, self.e2e_built, s)
        else:
            self.e2e_built(s)

    def e2e_built(self, s):
        e = s.gate.e2e
        s.e_run = 0
        s.e_phase = "startup"
        s.n_workers = max(1, min(self.Ce // self.w, self.accounts))
        self.eng.at(self.eng.t + e.startup_s, self.e2e_run, s)

    def e2e_run(self, s):
        e = s.gate.e2e
        s.e_phase = "run"
        tasks = [(d, self.w) for d in e.tests]

        def on_start(t):
            s.e_unstarted -= 1
            s.e_run += t[1]
            if s.e2e_first_test_start is None:
                s.e2e_first_test_start = self.eng.t

        def on_end(t):
            s.e_run -= t[1]
            s.e2e_end = self.eng.t

        def on_done():
            s.e_phase = "done"
            self.e_owner = None
            _check_done(s, self.eng)
            self.dispatch()
        s.lane = run_lanes(self.eng, tasks, s.n_workers, on_start, on_end, on_done)

    def live_workers(self, s):
        if s.e_phase == "startup":
            return s.n_workers
        if s.e_phase == "run":
            return s.n_workers if s.lane["i"] < len(s.gate.e2e.tests) else s.lane["run"]
        return 0

    def snapshot(self):
        arr = [s for s in self.gs if s.arrived]
        unstarted_any = any(s.u_unstarted > 0 or s.e_unstarted > 0 for s in arr)
        c = {k: 0 for k in CAUSES}
        busy_u = sum(s.u_run for s in self.gs)
        busy_e = sum(s.e_run for s in self.gs)
        o = self.u_owner
        idle = self.Cu - busy_u
        if o is None or o.u_phase == "queued":
            c["partition_boundary" if unstarted_any else "tail"] += idle
        elif o.u_phase == "overhead":
            c["startup"] += idle
        elif o.u_unstarted > 0:
            frag = self.Cu % 1000
            c["fragmentation"] += min(frag, idle)
            c["step_barrier" if self.arm == "A" else "fragmentation"] += idle - min(frag, idle)
        else:
            c["partition_boundary" if unstarted_any else "tail"] += idle
        o = self.e_owner
        idle = self.Ce - busy_e
        if o is None:
            c["partition_boundary" if unstarted_any else "tail"] += idle
        elif o.e_phase in ("overhead", "startup"):
            c["startup"] += idle
        elif o.e_phase == "build":
            c["e2e_prereq"] += idle
        elif o.e_unstarted > 0:
            fit = self.Ce // self.w
            n = o.n_workers
            cap_part = max(0, fit - n) * self.w if n >= self.accounts else 0
            c["account_cap"] += min(cap_part, idle)
            c["fragmentation"] += idle - min(cap_part, idle)
        else:
            c["partition_boundary" if unstarted_any else "tail"] += idle
        held = sum(self.live_workers(s) for s in self.gs)
        return busy_u, busy_e, c, unstarted_any, held

    def run(self):
        self.eng.run(lambda t: self.rec.add(t, *self.snapshot()))
        return self.gs, self.rec


class Pod:
    __slots__ = ("kind", "gate", "cpu", "prio", "seq", "state", "bound_t")

    def __init__(self, kind, gate, cpu, prio, seq):
        self.kind, self.gate, self.cpu, self.prio, self.seq = kind, gate, cpu, prio, seq
        self.state = "pending"
        self.bound_t = None


class PoolSim:
    def __init__(self, gates, w, accounts, pool, pod_overhead, sched_latency, slice_T, release_lag=0.0):
        self.w, self.accounts, self.P = m(w), accounts, m(pool)
        self.oh, self.lat, self.T, self.rlag = pod_overhead, sched_latency, slice_T, release_lag
        self.eng = Eng()
        self.gs = [GS(g, i) for i, g in enumerate(gates)]
        self.rec = Recorder()
        self.pending, self.bound = [], []
        self.seq = 0
        self.tokens = accounts
        self.tok_wait = []
        self.unit_q, self.e2e_q, self.released, self.build_pending = {}, {}, {}, {}
        for s in self.gs:
            self.eng.at(s.gate.arrival, self.arrive, s)

    def arrive(self, s):
        g = s.gate
        s.arrived = True
        self.unit_q[s.idx] = sorted(g.unit.all_tasks(), key=lambda d: -d)
        e = g.e2e
        self.e2e_q[s.idx] = list(e.tests) if e else []
        s.u_unstarted = len(self.unit_q[s.idx])
        has_build = bool(e and e.build_s > 0 and self.e2e_q[s.idx])
        self.build_pending[s.idx] = has_build
        s.e_unstarted = len(self.e2e_q[s.idx]) + (1 if has_build else 0)
        self.released[s.idx] = False
        s.u_phase = "run" if s.u_unstarted else "done"
        s.e_phase = "run" if s.e_unstarted else "done"
        if has_build:
            self.new_pod("build", s, 1000, 0)
        elif self.e2e_q[s.idx]:
            self.release(s)
        n_unit = min(s.u_unstarted, self.P // 1000)
        for _ in range(n_unit):
            self.new_pod("unit", s, 1000, 0)
        _check_done(s, self.eng)

    def release(self, s):
        self.released[s.idx] = True
        for _ in range(min(self.accounts, len(self.e2e_q[s.idx]))):
            self.new_pod("e2e", s, self.w, 1)

    def new_pod(self, kind, s, cpu, prio):
        p = Pod(kind, s, cpu, prio, self.seq)
        self.seq += 1
        self.pending.append(p)
        self.request_sched()
        return p

    def request_sched(self):
        self.eng.at(self.eng.t + self.lat, self.sched_pass)

    def free(self):
        return self.P - sum(p.cpu for p in self.bound)

    def sched_pass(self):
        free = self.free()
        for p in sorted(self.pending, key=lambda p: (p.prio, p.seq)):
            if p.cpu <= free:
                free -= p.cpu
                self.pending.remove(p)
                self.bound.append(p)
                p.state = "overhead"
                p.bound_t = self.eng.t + self.oh
                self.eng.at(self.eng.t + self.oh, self.started, p)

    def exit_pod(self, p, release_token=False):
        if release_token:
            self.tokens += 1
            self.grant_token()
        p.state = "exit"
        self.bound.remove(p)
        self.request_sched()

    def started(self, p):
        s = p.gate
        if p.kind == "unit":
            self.next_unit(p)
        elif p.kind == "build":
            self.build_pending[s.idx] = False
            s.e_unstarted -= 1
            s.e_run += 1000
            p.state = "run"
            self.eng.at(self.eng.t + s.gate.e2e.build_s, self.build_done, p)
        else:
            if not self.e2e_q[s.idx]:
                self.exit_pod(p)
                return
            p.state = "token_wait"
            self.tok_wait.append(p)
            self.grant_token()

    def build_done(self, p):
        s = p.gate
        s.e_run -= 1000
        self.exit_pod(p)
        if self.rlag > 0:
            self.eng.at(self.eng.t + self.rlag, self.release, s)
        else:
            self.release(s)

    def next_unit(self, p):
        s = p.gate
        q = self.unit_q[s.idx]
        if not q:
            self.exit_pod(p)
            return
        d = q.pop(0)
        s.u_unstarted -= 1
        s.u_run += 1000
        if s.unit_first_start is None:
            s.unit_first_start = self.eng.t
        p.state = "run"
        self.eng.at(self.eng.t + d, self.unit_done, p)

    def unit_done(self, p):
        s = p.gate
        s.u_run -= 1000
        s.unit_end = self.eng.t
        if s.u_unstarted == 0 and s.u_run == 0:
            s.u_phase = "done"
            _check_done(s, self.eng)
        self.next_unit(p)

    def grant_token(self):
        while self.tokens > 0 and self.tok_wait:
            p = self.tok_wait.pop(0)
            self.tokens -= 1
            p.state = "startup"
            self.eng.at(self.eng.t + p.gate.gate.e2e.startup_s, self.e2e_ready, p)

    def e2e_ready(self, p):
        if not self.e2e_q[p.gate.idx]:
            self.exit_pod(p, True)
            return
        self.next_test(p)

    def next_test(self, p):
        s = p.gate
        d = self.e2e_q[s.idx].pop(0)
        s.e_unstarted -= 1
        s.e_run += self.w
        if s.e2e_first_test_start is None:
            s.e2e_first_test_start = self.eng.t
        p.state = "run"
        self.eng.at(self.eng.t + d, self.test_done, p)

    def test_done(self, p):
        s = p.gate
        s.e_run -= self.w
        s.e2e_end = self.eng.t
        q = self.e2e_q[s.idx]
        if not q:
            self.exit_pod(p, True)
            if s.e_unstarted == 0 and s.e_run == 0:
                s.e_phase = "done"
                _check_done(s, self.eng)
            return
        if self.T is not None and self.eng.t - p.bound_t >= self.T:
            self.exit_pod(p, True)
            self.new_pod("e2e", s, self.w, 1)
            return
        self.next_test(p)

    def snapshot(self):
        arr = [s for s in self.gs if s.arrived]
        busy_u = sum(s.u_run for s in self.gs)
        busy_e = sum(s.e_run for s in self.gs)
        unstarted_any = any(s.u_unstarted > 0 or s.e_unstarted > 0 for s in arr)
        c = {k: 0 for k in CAUSES}
        for p in self.bound:
            if p.state == "overhead" or p.state == "startup":
                c["startup"] += p.cpu
            elif p.state == "token_wait":
                c["account_cap"] += p.cpu
        busy_pods = sum(p.cpu for p in self.bound if p.state == "run")
        held_idle = c["startup"] + c["account_cap"]
        free = self.P - busy_pods - held_idle
        if free > 0:
            if any(p.cpu <= free for p in self.pending):
                c["sched_latency"] += free
            elif not unstarted_any:
                c["tail"] += free
            else:
                req = []
                if any(self.unit_q[s.idx] for s in arr):
                    req.append(1000)
                if any(self.released[s.idx] and self.e2e_q[s.idx] for s in arr):
                    req.append(self.w)
                if not req:
                    c["e2e_prereq"] += free
                elif free < min(req):
                    c["fragmentation"] += free
                elif self.w in req:
                    spoken_for = any(p.kind == "e2e" and p.state in ("overhead", "startup") for p in self.bound)
                    c["startup" if spoken_for else "account_cap"] += free
                else:
                    c["other"] += free
        held = sum(1 for p in self.bound if p.kind == "e2e" and p.state in ("startup", "token_wait", "run"))
        return busy_u, busy_e, c, unstarted_any, held

    def run(self):
        self.eng.run(lambda t: self.rec.add(t, *self.snapshot()))
        return self.gs, self.rec


def summarize(arm, gs, rec, pool_milli, gates, keep_rows=False):
    rows = rec.rows
    t0 = min(s.gate.arrival for s in gs)
    t1 = max(s.end for s in gs)
    vac_idle = vac_t = idle_all = 0.0
    cause_vac = {k: 0.0 for k in CAUSES}
    cause_all = {k: 0.0 for k in CAUSES}
    held_dur = {}
    for i in range(len(rows) - 1):
        ta, tb = max(rows[i][0], t0), min(rows[i + 1][0], t1)
        dt = tb - ta
        if dt <= 0:
            continue
        _, bu, be, c, unst, held = rows[i]
        idle = pool_milli - bu - be
        assert idle == sum(c.values()), (arm, rows[i], idle)
        idle_all += idle * dt
        for k, v in c.items():
            cause_all[k] += v * dt / 1000
        if unst:
            vac_idle += idle * dt
            vac_t += dt
            for k, v in c.items():
                cause_vac[k] += v * dt / 1000
        held_dur[held] = held_dur.get(held, 0.0) + dt
    pos = {h: d for h, d in held_dur.items() if h > 0}
    tot = sum(pos.values())
    med, acc = 0, 0.0
    for h in sorted(pos):
        acc += pos[h]
        if acc >= tot / 2:
            med = h
            break
    res = {
        "arm": arm, "pool_cpu": pool_milli / 1000, "window_s": t1 - t0, "t_end": t1,
        "vacuum_share": vac_idle / (pool_milli * vac_t) if vac_t > 0 else 0.0,
        "vacuum_window_s": vac_t,
        "idle_share": idle_all / (pool_milli * (t1 - t0)),
        "vacuum_cause_cpu_s": cause_vac, "idle_cause_cpu_s": cause_all,
        "held_max": max(pos) if pos else 0, "held_median": med,
        "gates": [],
    }
    for s in gs:
        ue = s.unit_end if s.unit_end is not None else s.gate.arrival
        ee = s.e2e_end if s.e2e_end is not None else s.gate.arrival
        res["gates"].append({
            "arrival": s.gate.arrival, "unit_first_start": s.unit_first_start,
            "unit_start_delay": (s.unit_first_start - s.gate.arrival) if s.unit_first_start is not None else None,
            "unit_end": ue - s.gate.arrival, "e2e_end": ee - s.gate.arrival,
            "e2e_first_test_start": (s.e2e_first_test_start - s.gate.arrival) if s.e2e_first_test_start is not None else None,
            "wall": s.end - s.gate.arrival})
    g0 = res["gates"][0]
    res.update(wall=g0["wall"], unit_end=g0["unit_end"], e2e_end=g0["e2e_end"])
    if keep_rows:
        res["rows"] = [(t, bu, be, dict(c), u, h) for t, bu, be, c, u, h in rows]
    return res


def simulate(arm, gates, w=1.0, accounts=4, unit_cpu=4.0, pool=8.0, pod_overhead=0.0, sched_latency=0.0,
             slice_T=None, a_conc=(2, 2, 2), keep_rows=False, release_lag=0.0):
    if arm in ("A", "B"):
        sim = PartSim(arm, gates, w, accounts, unit_cpu, pool, pod_overhead, a_conc)
    elif arm == "C":
        sim = PoolSim(gates, w, accounts, pool, pod_overhead, sched_latency, slice_T, release_lag)
    else:
        raise ValueError(arm)
    gs, rec = sim.run()
    return summarize(arm, gs, rec, m(pool), gates, keep_rows)


def timeline(res, step):
    rows = res["rows"]
    pool = res["pool_cpu"]
    out = []
    t = 0.0
    end = rows[-1][0]
    while t < end:
        tb = min(t + step, end)
        su = se = sh = 0.0
        for i in range(len(rows) - 1):
            ta, tbb = max(rows[i][0], t), min(rows[i + 1][0], tb)
            if tbb > ta:
                su += rows[i][1] * (tbb - ta)
                se += rows[i][2] * (tbb - ta)
                sh += rows[i][5] * (tbb - ta)
        d = tb - t
        out.append({"t": t, "unit_cpu": su / 1000 / d, "e2e_cpu": se / 1000 / d,
                    "idle": pool - su / 1000 / d - se / 1000 / d, "held": sh / d})
        t = tb
    return out


def scenario(arm, unit_model, unit_run, e2e_model, e2e_run, offsets=(0.0,), build_lanes=1, **kw):
    u, e = load_unit(unit_model, unit_run, build_lanes), load_e2e(e2e_model, e2e_run)
    return simulate(arm, [Gate(o, u, e) for o in offsets], **kw)
