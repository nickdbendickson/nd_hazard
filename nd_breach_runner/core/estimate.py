"""How long will a run take? An estimate, not a promise.

LISFLOOD-FP's time step is proportional to the cell size, so the cost of a run
is about (cells x simulated seconds) / cell size. `lisflood.calibrate` measures
this computer's speed on a small synthetic flood; the estimate scales it to
the dam's first domain and adds the usual extra runs when the domain has to
grow. Deep, fast water (a tall dam) shortens the time step and makes a run
slower than the calibration case, so the range given is wide.
"""

DEEP_WATER_FACTOR = 1.3      # typical breach floods are deeper than the calibration flood
EXTENSION_RUNS = (1.0, 1.8, 3.0)      # sizes of a first, second, third run relative to the first


def seconds_for(cells, sim_time_s, cell_m, rate, cal_cell_m=10.0):
    return DEEP_WATER_FACTOR * cells * sim_time_s / rate * (cal_cell_m / cell_m)


def estimate(plan, rate, tier_both=False, cal_cell_m=10.0):
    """plan from workflow.preview(). Returns a dict of minutes."""
    one = seconds_for(plan["cells"], plan["sim_time_s"], plan["cell_m"], rate, cal_cell_m)
    lo = one
    typ = one * sum(EXTENSION_RUNS[:2])
    hi = one * sum(EXTENSION_RUNS[:3])
    if plan.get("baseline"):
        lo, typ, hi = lo + one, typ + one * EXTENSION_RUNS[1], hi + one * EXTENSION_RUNS[2]
    out = {"one_run_min": round(one / 60.0, 1), "low_min": round(lo / 60.0, 1),
           "typical_min": round(typ / 60.0, 1), "high_min": round(hi / 60.0, 1)}
    if tier_both:
        out["note_anuga"] = ("ANUGA adds to this: from a few minutes to several hours, "
                             "depending on the mesh; it is not estimated here.")
    return out


def describe(plan, est):
    s = ["First domain {:.1f} x {:.1f} mi, {:,} cells at {:g} m, {:.1f} h simulated.".format(
        plan["size_mi"][0], plan["size_mi"][1], plan["cells"], plan["cell_m"],
        plan["sim_time_s"] / 3600.0),
        "One LISFLOOD-FP run: about {}. Expect {}-{} min in all if the domain has to grow "
        "(typical {} min){}.".format(
            "under a minute" if est["one_run_min"] < 0.5 else "{} min".format(est["one_run_min"]),
            est["low_min"], est["high_min"], est["typical_min"],
            "; the no-breach baseline runs are included" if plan.get("baseline") else "")]
    if est.get("note_anuga"):
        s.append(est["note_anuga"])
    if plan["cells"] > 40e6:
        s.append("That is a very large model ({:,} cells): use a coarser cell size for the "
                 "first pass.".format(plan["cells"]))
    return s
