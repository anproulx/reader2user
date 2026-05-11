import os
import json
import csv
import random
from pathlib import Path

# STEP: Create a simulated, reproducible dataset for RBC-like analysis
# Folder structure:
# ./dataset/
#   sub-subject/ (e.g., sub-01)
#     demographics.json
#     behavioral.csv
#     notes.txt
#     anat/
#       parcellation.tsv
#     func/
#       timeseries.csv

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def write_demographics(sub_path: Path, subject_id: str, rnd: random.Random):
    demog = {
        "subject_id": subject_id,
        "age": rnd.randint(20, 65),
        "sex": rnd.choice(["M", "F"]),
        "handedness": rnd.choice(["Right", "Left", "Ambidextrous"]),
        "education_years": rnd.randint(12, 20)
    }
    demog_path = sub_path / "demographics.json"
    with open(demog_path, "w") as f:
        json.dump(demog, f, indent=2)
    return demog

def write_behavioral(sub_path: Path, subject_id: str, rnd: random.Random, n_trials: int = 60):
    beh_path = sub_path / "behavioral.csv"
    headers = ["trial", "condition", "reaction_time_ms", "accuracy", "session"]
    conditions = ["stim_A", "stim_B", "stim_C"]
    with open(beh_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        for t in range(1, n_trials + 1):
            cond = rnd.choice(conditions)
            rt = int(max(120, rnd.gauss(350, 60)))
            acc = max(0.4, min(1.0, rnd.gauss(0.9, 0.05)))
            session = "session-1" if t <= n_trials // 2 else "session-2"
            writer.writerow([t, cond, rt, round(acc, 3), session])
    return

def write_parcellation(sub_path: Path, rnd: random.Random, n_regions: int = 100):
    anat_dir = sub_path / "anat"
    ensure_dir(anat_dir)
    parcell_path = anat_dir / "parcellation.tsv"
    with open(parcell_path, "w", newline="") as f:
        f.write("region_id\tthickness_mm\n")
        for i in range(1, n_regions + 1):
            region_id = f"ROI-{i:03d}"
            thickness = rnd.gauss(2.6, 0.25)
            thickness = max(1.6, min(3.6, thickness))
            f.write(f"{region_id}\t{thickness:.3f}\n")
    return

def write_timeseries(sub_path: Path, rnd: random.Random, n_timepoints: int = 200, n_rois: int = 5):
    func_dir = sub_path / "func"
    ensure_dir(func_dir)
    ts_path = func_dir / "timeseries.csv"
    header = ["time"] + [f"ROI-{i:03d}" for i in range(1, n_rois + 1)]
    with open(ts_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for t in range(1, n_timepoints + 1):
            row = [t] + [round(rnd_value(rnd), 3) for _ in range(n_rois)]
            writer.writerow(row)
    return

def rnd_value(rnd: random.Random, mean=0.0, std=1.0):
    # standard normal-like values with a small mean shift to feel realistic
    return rnd.gauss(mean, std)

def write_notes(sub_path: Path):
    notes_path = sub_path / "notes.txt"
    with open(notes_path, "w") as f:
        f.write("This is a simulated RBC-like dataset for demonstration purposes.\n")
        f.write("Subject folders contain demographics, behavioral metrics, and mock imaging data.\n")
    return

def main():
    base_dir = Path("./dataset")
    ensure_dir(base_dir)

    # Define subjects (sub-01 to sub-06)
    subject_ids = [f"sub-{i:02d}" for i in range(1, 7)]

    # Global random seed for overall reproducibility
    global_seed = 20240601
    master_rng = random.Random(global_seed)

    for idx, sub_id in enumerate(subject_ids, start=1):
        # Per-subject RNG seeded for reproducibility
        per_sub_seed = global_seed + idx
        rnd = random.Random(per_sub_seed)

        sub_path = base_dir / sub_id
        ensure_dir(sub_path)

        # 1) Demographics
        demog = write_demographics(sub_path, sub_id, rnd)

        # 2) Behavioral data
        n_trials = rnd.randint(40, 80)
        write_behavioral(sub_path, sub_id, rnd, n_trials=n_trials)

        # 3) Anatomy parcellation (parcellation.tsv)
        write_parcellation(sub_path, rnd, n_regions=100)

        # 4) Functional timeseries (timeseries.csv)
        write_timeseries(sub_path, rnd, n_timepoints=200, n_rois=5)

        # 5) Notes
        write_notes(sub_path)

        # Optional: summary log (printed only when running script)
        print(f"Generated data for {sub_id} (n_trials={n_trials}, regions=100, timepoints=200)")

    print("SUCCESS: Full dataset generated.")

if __name__ == "__main__":
    main()