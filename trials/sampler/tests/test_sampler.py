from trials.sampler.gaps import largest_gap, main as gaps_main
from trials.sampler.sample import busy_fraction, cpu_times, mem_used_mib, run

STAT_A = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1 1 1 1 1 1 1\n"
STAT_B = "cpu  200 0 200 1300 300 0 0 0 0 0\ncpu0 1 1 1 1 1 1 1 1 1 1\n"
MEMINFO = "MemTotal:       8192000 kB\nMemFree:        1000000 kB\nMemAvailable:   6144000 kB\n"


def test_cpu_times_count_iowait_as_idle():
    assert cpu_times(STAT_A) == (1000, 800)


def test_busy_fraction_hand_value():
    assert busy_fraction(cpu_times(STAT_A), cpu_times(STAT_B)) == round((1000 - 800) / 1000, 4)


def test_mem_used_is_total_minus_available():
    assert mem_used_mib(MEMINFO) == ((8192000 - 6144000) // 1024, 8192000 // 1024)


def test_run_writes_header_and_rows(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(STAT_A)
    (proc / "meminfo").write_text(MEMINFO)
    ticks = iter([10.0, 11.0])

    def sleep(_):
        (proc / "stat").write_text(STAT_B)

    out = tmp_path / "s.csv"
    run(out, 1.0, proc=proc, clock=lambda: next(ticks), sleep=sleep, limit=2)
    lines = out.read_text().splitlines()
    assert lines[0] == "epoch,cpu_busy,mem_used_mib,mem_total_mib"
    assert lines[1] == "10.000,0.2,2000,8000"
    assert lines[2] == "11.000,0.0,2000,8000"


def test_largest_gap_and_exit_codes(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("epoch,cpu_busy,mem_used_mib,mem_total_mib\n0,0,0,0\n1,0,0,0\n3.5,0,0,0\n")
    bad = tmp_path / "bad.csv"
    bad.write_text("epoch,cpu_busy,mem_used_mib,mem_total_mib\n0,0,0,0\n1,0,0,0\n4.5,0,0,0\n")
    assert largest_gap(good.read_text()) == (2.5, 3)
    assert gaps_main([str(good)]) == 0
    assert gaps_main([str(bad)]) == 1
    assert gaps_main([str(tmp_path / "missing.csv")]) == 1
