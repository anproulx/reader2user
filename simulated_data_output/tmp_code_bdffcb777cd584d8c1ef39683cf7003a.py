import os
import json
import csv
import random
import math

# Step 1: Configuration
NUM_SUBJECTS = 12
TOP_DIR = "./dataset"
ROIS = 5

# Ensure reproducibility
RANDOM_SEED = 42
random.seed(RANDOM_SEED)

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def write_demographics(sub_dir, subject_id):
    # Random demographic attributes
    age = random.randint(8, 65)
    sex = random.choice(["M", "F"])
    group = random.choice(["control", "patient"])
    handedness = random.choice(["R", "L", "ambi"])
    demo = {
        "subject_id": subject_id,
        "age": age,
        "sex": sex,
        "group": group,
        "handedness": handedness
    }
    with open(os.path.join(sub_dir, "demographics.json"), "w") as f:
        json.dump(demo, f, indent=2)

def write_metrics(sub_dir, subject_id):
    # Generate plausible cortical thickness values for 5 ROIs
    base_thickness = [random.uniform(2.4, 3.6) for _ in range(ROIS)]
    thickness = [round(v + random.uniform(-0.25, 0.25), 3) for v in base_thickness]
    icv = random.randint(90000, 150000)  # intracranial volume in mm^3
    total_mean_thickness = round(sum(thickness) / ROIS, 3)

    header = [f"roi_{i+1}_thickness_mm" for i in range(ROIS)]
    header.extend(["icv_mm3", "total_mean_thickness_mm"])
    row = thickness + [icv, total_mean_thickness]

    with open(os.path.join(sub_dir, "metrics.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerow(row)

def write_timeseries(sub_dir, subject_id):
    # Generate a 100-point timeseries for 5 ROIs
    n_tp = 100
    header = ["time"] + [f"region_{i+1}" for i in range(ROIS)]
    rows = []
    # Seed per subject for reproducibility
    random.seed(1234 + int(subject_id.split("-")[1]))
    for t in range(n_tp):
        row = [t]
        for r in range(ROIS):
            base = random.uniform(0.5, 1.5)
            freq = random.uniform(0.05, 0.15)
            phase = random.uniform(0, 3)
            noise = random.gauss(0, 0.1)
            val = base * (1.0 + 0.5 * math.sin(2 * math.pi * freq * t + phase)) + noise
            row.append(round(val, 4))
        rows.append(row)

    with open(os.path.join(sub_dir, "timeseries.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

def write_study_info(sub_dir, subject_id):
    # Simple human-readable summary
    with open(os.path.join(sub_dir, "study_info.txt"), "w") as f:
        f.write(f"subject_id: {subject_id}\n")
        f.write("status: simulated dataset\n")

def main():
    # Create top-level dataset directory
    ensure_dir(TOP_DIR)

    for idx in range(1, NUM_SUBJECTS + 1):
        sub_id = f"sub-{idx:02d}"
        sub_dir = os.path.join(TOP_DIR, sub_id)
        ensure_dir(sub_dir)

        # Generate files per subject
        write_demographics(sub_dir, sub_id)
        write_metrics(sub_dir, sub_id)
        write_timeseries(sub_dir, sub_id)
        write_study_info(sub_dir, sub_id)

    print("SUCCESS: Full dataset generated.")

if __name__ == "__main__":
    main()