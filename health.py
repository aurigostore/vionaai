"""Perhitungan kesehatan yang deterministik (supaya angka tidak dikarang AI)."""

from collections import defaultdict

import db as dbmod

ACTIVITY_FACTORS = {
    "sedentari": 1.2,      # kerja duduk, hampir tidak olahraga
    "ringan": 1.375,       # olahraga 1-3x/minggu
    "sedang": 1.55,        # olahraga 3-5x/minggu
    "berat": 1.725,        # olahraga 6-7x/minggu
    "sangat_berat": 1.9,   # atlet / kerja fisik berat
}

GOAL_ADJUST = {
    "turun_berat": -500,
    "jaga_berat": 0,
    "naik_otot": 300,
    "naik_berat": 500,
}

PROTEIN_PER_KG = {
    "turun_berat": (1.8, 2.2),
    "jaga_berat": (1.2, 1.6),
    "naik_otot": (1.6, 2.2),
    "naik_berat": (1.6, 2.0),
}


def compute_needs(profile: dict) -> dict:
    required = ["jenis_kelamin", "tahun_lahir", "tinggi_cm", "berat_kg"]
    missing = [f for f in required if not profile.get(f)]
    if missing:
        return {"error": "Profil kesehatan belum lengkap", "field_kurang": missing}

    weight = float(profile["berat_kg"])
    height = float(profile["tinggi_cm"])
    age = dbmod.now().year - int(profile["tahun_lahir"])
    male = str(profile["jenis_kelamin"]).lower() in ("pria", "laki-laki", "l", "male")
    activity = profile.get("tingkat_aktivitas", "ringan")
    goal = profile.get("tujuan", "jaga_berat")

    # Mifflin-St Jeor
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if male else -161)
    tdee = bmr * ACTIVITY_FACTORS.get(activity, 1.375)
    target_kcal = tdee + GOAL_ADJUST.get(goal, 0)
    p_low, p_high = PROTEIN_PER_KG.get(goal, (1.2, 1.6))
    bmi = weight / (height / 100) ** 2

    if bmi < 18.5:
        bmi_cat = "kurus"
    elif bmi < 23:
        bmi_cat = "normal (standar Asia)"
    elif bmi < 25:
        bmi_cat = "berisiko / overweight (standar Asia)"
    else:
        bmi_cat = "obesitas (standar Asia)"

    return {
        "umur": age,
        "bmi": round(bmi, 1),
        "kategori_bmi": bmi_cat,
        "bmr_kkal": round(bmr),
        "tdee_kkal": round(tdee),
        "target_kalori_kkal": round(target_kcal),
        "protein_gram_per_hari": [round(weight * p_low), round(weight * p_high)],
        "air_ml_per_hari_dasar": round(weight * 35),
        "air_tambahan_saat_olahraga": "+500-750 ml per jam olahraga",
        "asumsi": {"tingkat_aktivitas": activity, "tujuan": goal},
        "catatan": "Estimasi umum, bukan diagnosis medis.",
    }


def summarize_logs(logs: list[dict]) -> dict:
    per_day = defaultdict(lambda: defaultdict(float))
    weights = []
    workouts = 0
    workout_minutes = 0.0
    for log in logs:
        day = log["logged_at"][:10]
        t = log["type"]
        if t == "berat" and log["value"] is not None:
            weights.append((log["logged_at"], log["value"]))
        elif t == "olahraga":
            workouts += 1
            workout_minutes += log["value"] or 0
        if log["value"] is not None and t in ("air", "makanan", "langkah", "tidur"):
            per_day[day][t] += log["value"]

    summary: dict = {
        "total_harian": {d: dict(v) for d, v in sorted(per_day.items())},
        "jumlah_sesi_olahraga": workouts,
        "total_menit_olahraga": workout_minutes,
    }
    if weights:
        summary["berat"] = {
            "awal": weights[0][1],
            "terakhir": weights[-1][1],
            "perubahan_kg": round(weights[-1][1] - weights[0][1], 2),
        }
    return summary
