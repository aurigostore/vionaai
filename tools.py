"""Definisi tools yang bisa dipanggil Claude, beserta eksekusinya."""

import html
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import config
import health
from db import Database, now

# ---------------------------------------------------------------------------
# Skema tools (format Anthropic Messages API)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "simpan_memori",
        "description": (
            "Simpan fakta penting dan tahan lama tentang user ke memori jangka panjang: "
            "identitas, pekerjaan, keluarga, preferensi, kebiasaan, tujuan, masalah yang sedang "
            "dihadapi, kondisi kesehatan, alergi, dll. Satu fakta per panggilan, ditulis ringkas "
            "sebagai kalimat orang ketiga (mis. 'User alergi udang'). Jangan simpan hal sepele "
            "atau yang sudah ada di memori; kalau fakta lama berubah, pakai ubah_memori."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "isi": {"type": "string", "description": "Fakta yang disimpan"},
                "kategori": {
                    "type": "string",
                    "enum": ["identitas", "pekerjaan", "keluarga", "preferensi", "kebiasaan",
                             "tujuan", "masalah", "kesehatan", "keuangan", "umum"],
                },
            },
            "required": ["isi", "kategori"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ubah_memori",
        "description": "Perbarui isi memori yang sudah ada (pakai id dari daftar memori) bila faktanya berubah.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "isi": {"type": "string"}},
            "required": ["id", "isi"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hapus_memori",
        "description": "Hapus memori yang salah, kedaluwarsa, atau diminta user untuk dilupakan.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cari_memori",
        "description": "Cari memori berdasarkan kata kunci (berguna bila memori sudah sangat banyak).",
        "input_schema": {
            "type": "object",
            "properties": {"kata_kunci": {"type": "string"}},
            "required": ["kata_kunci"],
            "additionalProperties": False,
        },
    },
    {
        "name": "atur_profil_kesehatan",
        "description": (
            "Simpan/perbarui data fisik & target kesehatan user. Isi hanya field yang diketahui. "
            "Panggil juga saat berat badan terbaru diketahui."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jenis_kelamin": {"type": "string", "enum": ["pria", "wanita"]},
                "tahun_lahir": {"type": "integer"},
                "tinggi_cm": {"type": "number"},
                "berat_kg": {"type": "number"},
                "tingkat_aktivitas": {
                    "type": "string",
                    "enum": ["sedentari", "ringan", "sedang", "berat", "sangat_berat"],
                },
                "tujuan": {"type": "string", "enum": ["turun_berat", "jaga_berat", "naik_otot", "naik_berat"]},
                "target_berat_kg": {"type": "number"},
                "kondisi_medis": {"type": "string", "description": "Kondisi/riwayat medis, cedera, alergi"},
                "catatan": {"type": "string", "description": "Info lain: jadwal gym, pantangan makanan, dll"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "hitung_kebutuhan_harian",
        "description": (
            "Hitung BMI, BMR, TDEE, target kalori, protein, dan kebutuhan air harian dari profil "
            "kesehatan user. Selalu pakai tool ini untuk angka-angka tersebut, jangan menghitung sendiri."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "catat_kesehatan",
        "description": (
            "Catat aktivitas/kondisi kesehatan. Satuan baku: air=ml, berat=kg, makanan=kkal (estimasi), "
            "olahraga=menit (detail latihan di catatan, mis. 'bench press 60kg 3x8'), tidur=jam, "
            "langkah=langkah, lainnya=bebas (mis. tekanan darah, mood)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jenis": {"type": "string", "enum": ["air", "berat", "makanan", "olahraga", "tidur", "langkah", "lainnya"]},
                "nilai": {"type": "number", "description": "Angka sesuai satuan baku (boleh kosong untuk 'lainnya')"},
                "catatan": {"type": "string"},
                "waktu": {"type": "string", "description": "ISO datetime bila bukan sekarang, mis. 2026-10-21T07:00"},
            },
            "required": ["jenis"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hapus_catatan_kesehatan",
        "description": "Hapus catatan kesehatan yang salah input (pakai id dari lihat_catatan_kesehatan).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lihat_catatan_kesehatan",
        "description": "Ambil catatan kesehatan N hari terakhir beserta ringkasan (total harian, tren berat, sesi olahraga).",
        "input_schema": {
            "type": "object",
            "properties": {
                "hari": {"type": "integer", "description": "Berapa hari ke belakang (default 7)"},
                "jenis": {"type": "string", "enum": ["air", "berat", "makanan", "olahraga", "tidur", "langkah", "lainnya"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "buat_pengingat",
        "description": (
            "Jadwalkan pengingat ke Telegram user. jenis='sekali' butuh waktu (ISO datetime lokal). "
            "jenis='harian' butuh jam (HH:MM) dan opsional hari (1=Senin ... 7=Minggu). "
            "Untuk pengingat berulang tiap N jam, buat beberapa pengingat harian di jam-jam berbeda. "
            "pakai_ai=true bila pesan perlu disusun AI saat waktunya tiba (mis. cek progres minum hari "
            "itu, motivasi gym dengan konteks terbaru)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pesan": {"type": "string", "description": "Isi pengingat, atau instruksi untuk AI bila pakai_ai"},
                "jenis": {"type": "string", "enum": ["sekali", "harian"]},
                "waktu": {"type": "string"},
                "jam": {"type": "string"},
                "hari": {"type": "array", "items": {"type": "integer"}},
                "pakai_ai": {"type": "boolean"},
            },
            "required": ["pesan", "jenis"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lihat_pengingat",
        "description": "Daftar pengingat yang aktif.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "hapus_pengingat",
        "description": "Matikan pengingat berdasarkan id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cari_internet",
        "description": "Cari informasi terbaru di internet (berita, harga, fakta terkini, referensi). Kembalikan judul, link, dan cuplikan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kueri": {"type": "string"},
                "jumlah": {"type": "integer", "description": "Jumlah hasil, default 5, maks 10"},
            },
            "required": ["kueri"],
            "additionalProperties": False,
        },
    },
    {
        "name": "buka_halaman",
        "description": "Ambil isi teks sebuah halaman web (dari link hasil pencarian atau yang dikirim user).",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------------
# Eksekusi
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    db: Database
    user_id: int
    chat_id: int
    schedule_reminder: Callable[[dict], None]
    unschedule_reminder: Callable[[int], None]


def _parse_local_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TIMEZONE)
    return dt


def _web_search(query: str, n: int) -> list[dict]:
    from ddgs import DDGS

    # DuckDuckGo diblokir di sebagian jaringan Indonesia, jadi coba beberapa backend
    last_error: Exception | None = None
    for backend in ("auto", "yahoo", "brave", "mojeek"):
        try:
            results = DDGS().text(query, max_results=max(1, min(n, 10)), backend=backend)
        except Exception as e:
            last_error = e
            continue
        if results:
            return [{"judul": r.get("title"), "url": r.get("href"), "cuplikan": r.get("body")} for r in results]
    raise RuntimeError(f"Pencarian gagal: {last_error}")


def _fetch_page(url: str, max_chars: int = 12000) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL harus diawali http:// atau https://")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal-assistant-bot)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(2_000_000).decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:max_chars] + ("\n...[dipotong]" if len(text) > max_chars else "")


def execute(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    """Jalankan tool dan kembalikan hasil sebagai string (JSON). Error dilempar sebagai exception."""
    db, uid = ctx.db, ctx.user_id

    if name == "simpan_memori":
        mid = db.add_memory(uid, args["isi"].strip(), args.get("kategori", "umum"))
        return json.dumps({"ok": True, "id": mid})

    if name == "ubah_memori":
        return json.dumps({"ok": db.update_memory(uid, int(args["id"]), args["isi"].strip())})

    if name == "hapus_memori":
        return json.dumps({"ok": db.delete_memory(uid, int(args["id"]))})

    if name == "cari_memori":
        return json.dumps(db.search_memories(uid, args["kata_kunci"]), ensure_ascii=False)

    if name == "atur_profil_kesehatan":
        profile = db.update_health_profile(uid, args)
        if "berat_kg" in args:
            db.add_health_log(uid, "berat", args["berat_kg"], "kg", "dari update profil", now().isoformat(timespec="seconds"))
        return json.dumps({"ok": True, "profil": profile}, ensure_ascii=False)

    if name == "hitung_kebutuhan_harian":
        return json.dumps(health.compute_needs(db.get_health_profile(uid)), ensure_ascii=False)

    if name == "catat_kesehatan":
        units = {"air": "ml", "berat": "kg", "makanan": "kkal", "olahraga": "menit",
                 "tidur": "jam", "langkah": "langkah"}
        when = _parse_local_dt(args["waktu"]) if args.get("waktu") else now()
        log_id = db.add_health_log(uid, args["jenis"], args.get("nilai"), units.get(args["jenis"]),
                                   args.get("catatan"), when.isoformat(timespec="seconds"))
        if args["jenis"] == "berat" and args.get("nilai"):
            db.update_health_profile(uid, {"berat_kg": args["nilai"]})
        today = now().date().isoformat()
        logs_today = db.get_health_logs(uid, today, args["jenis"])
        total_today = sum(l["value"] or 0 for l in logs_today)
        return json.dumps({"ok": True, "id": log_id, f"total_{args['jenis']}_hari_ini": total_today})

    if name == "hapus_catatan_kesehatan":
        return json.dumps({"ok": db.delete_health_log(uid, int(args["id"]))})

    if name == "lihat_catatan_kesehatan":
        days = int(args.get("hari") or 7)
        since = (now() - timedelta(days=days)).date().isoformat()
        logs = db.get_health_logs(uid, since, args.get("jenis"))
        return json.dumps({"ringkasan": health.summarize_logs(logs), "catatan": logs[-100:]}, ensure_ascii=False)

    if name == "buat_pengingat":
        kind = args["jenis"]
        run_at = time_of_day = days = None
        if kind == "sekali":
            if not args.get("waktu"):
                raise ValueError("Pengingat 'sekali' butuh field waktu")
            dt = _parse_local_dt(args["waktu"])
            if dt <= now():
                raise ValueError(f"Waktu {dt.isoformat()} sudah lewat")
            run_at = dt.isoformat(timespec="seconds")
        else:
            if not args.get("jam") or not re.fullmatch(r"\d{1,2}:\d{2}", args["jam"]):
                raise ValueError("Pengingat 'harian' butuh field jam format HH:MM")
            time_of_day = args["jam"]
            days = sorted({int(d) for d in args.get("hari") or [] if 1 <= int(d) <= 7}) or None
        rid = db.add_reminder(uid, ctx.chat_id, args["pesan"], kind, run_at, time_of_day, days,
                              bool(args.get("pakai_ai")))
        reminder = next(r for r in db.list_reminders(uid) if r["id"] == rid)
        ctx.schedule_reminder(reminder)
        return json.dumps({"ok": True, "id": rid})

    if name == "lihat_pengingat":
        return json.dumps(db.list_reminders(uid), ensure_ascii=False)

    if name == "hapus_pengingat":
        ok = db.deactivate_reminder(uid, int(args["id"]))
        if ok:
            ctx.unschedule_reminder(int(args["id"]))
        return json.dumps({"ok": ok})

    if name == "cari_internet":
        return json.dumps(_web_search(args["kueri"], int(args.get("jumlah") or 5)), ensure_ascii=False)

    if name == "buka_halaman":
        return _fetch_page(args["url"])

    raise ValueError(f"Tool tidak dikenal: {name}")
