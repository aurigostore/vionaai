"""Otak agent: menyusun konteks, memanggil Claude, dan menjalankan loop tool."""

import asyncio
import json
import logging

import anthropic

import config
import tools
from db import Database, now

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15

PERSONA = """Kamu adalah asisten pribadi user di Telegram: teman ngobrol, penasihat, dan pelatih kesehatan yang mengenal user secara mendalam.

Cara kerja:
- Gunakan bahasa Indonesia yang santai dan hangat, kecuali user memakai bahasa lain.
- Kamu punya memori jangka panjang (lihat bagian MEMORI di bawah). Pakai memori itu agar jawaban terasa personal, tanpa perlu menyebut "menurut memori saya".
- Setiap kali user menyebut fakta baru yang tahan lama tentang dirinya (identitas, pekerjaan, keluarga, preferensi, kebiasaan, tujuan, masalah, kondisi kesehatan), simpan dengan simpan_memori. Kalau fakta lama berubah, pakai ubah_memori. Jangan simpan obrolan sepele.
- Untuk masalah pribadi: dengarkan dulu, pahami konteksnya, ajukan pertanyaan bila perlu, baru beri saran yang konkret dan bisa dijalankan.
- Untuk kesehatan, gizi, dan gym: pakai profil kesehatan dan catatan user. Angka BMI/kalori/protein/air selalu dari hitung_kebutuhan_harian, bukan hitungan sendiri. Kalau profil belum lengkap, tanyakan yang kurang lalu simpan dengan atur_profil_kesehatan. Saat user melaporkan minum, makan, berat, tidur, atau latihan, catat dengan catat_kesehatan. Buat program latihan yang progresif dan aman; untuk keluhan medis, nyeri, atau gejala serius, sarankan periksa ke dokter.
- Untuk info terkini (berita, harga, jadwal, fakta yang bisa berubah), pakai cari_internet dan buka_halaman, lalu sebutkan sumbernya.
- Untuk pengingat, pakai buat_pengingat dengan waktu lokal user. Konfirmasi jadwal yang dibuat.
- Jawaban ringkas dan enak dibaca di HP. Format yang didukung: **tebal**, _miring_, `kode`, daftar dengan "-". Jangan pakai tabel atau heading #.
- Jangan pernah meminta atau menyimpan password, PIN, OTP, atau nomor kartu."""


def _dynamic_context(db: Database, user_id: int) -> str:
    memories = db.list_memories(user_id)
    profile = db.get_health_profile(user_id)
    mem_text = "\n".join(f"[{m['id']}] ({m['category']}) {m['content']}" for m in memories) or "(belum ada)"
    profile_text = json.dumps(profile, ensure_ascii=False) if profile else "(belum diisi)"
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    t = now()
    return (
        f"WAKTU SEKARANG: {days[t.weekday()]}, {t.strftime('%Y-%m-%d %H:%M')} ({config.TIMEZONE.key})\n\n"
        f"MEMORI TENTANG USER (format [id] (kategori) isi):\n{mem_text}\n\n"
        f"PROFIL KESEHATAN:\n{profile_text}"
    )


def _normalize_history(rows: list[dict]) -> list[dict]:
    """Pastikan riwayat diawali 'user' dan peran selalu bergantian."""
    messages: list[dict] = []
    for row in rows:
        if messages and messages[-1]["role"] == row["role"]:
            messages[-1]["content"] += "\n\n" + row["content"]
        else:
            messages.append({"role": row["role"], "content": row["content"]})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        # pesan user terakhir tanpa balasan (mis. error sebelumnya); gabungkan ke giliran baru
        messages.pop()
    return messages


class Agent:
    def __init__(self, db: Database):
        self.db = db
        self.client = anthropic.AsyncAnthropic(
            base_url=config.AI_BASE_URL,
            api_key=config.AI_API_KEY,
            timeout=300.0,
            max_retries=3,
        )
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def respond(
        self,
        ctx: tools.ToolContext,
        content: list[dict],
        history_text: str,
    ) -> str:
        """Proses satu giliran user. `content` = blok konten untuk Claude (teks/gambar/dokumen),
        `history_text` = versi teks yang disimpan ke riwayat."""
        async with self._lock(ctx.user_id):
            history = _normalize_history(self.db.get_history(ctx.user_id, config.HISTORY_LIMIT))
            messages = history + [{"role": "user", "content": content}]
            system = [
                {"type": "text", "text": PERSONA, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _dynamic_context(self.db, ctx.user_id)},
            ]

            reply = await self._run_loop(ctx, system, messages)

            self.db.add_message(ctx.user_id, "user", history_text)
            self.db.add_message(ctx.user_id, "assistant", reply)
            return reply

    async def _run_loop(self, ctx: tools.ToolContext, system: list[dict], messages: list[dict]) -> str:
        extra = {"output_config": {"effort": config.AI_EFFORT}} if config.AI_EFFORT else {}
        response = None
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self.client.messages.create(
                model=config.AI_MODEL,
                max_tokens=config.AI_MAX_TOKENS,
                system=system,
                tools=tools.TOOLS,
                messages=messages,
                **extra,
            )

            if response.stop_reason == "refusal":
                return "Maaf, permintaan ini tidak bisa saya bantu. Coba ungkapkan dengan cara lain ya."

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content})
            results = await asyncio.gather(*(self._run_tool(ctx, tu) for tu in tool_uses))
            messages.append({"role": "user", "content": list(results)})
        else:
            log.warning("Batas putaran tool tercapai untuk user %s", ctx.user_id)

        text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n_(jawaban terpotong karena terlalu panjang)_"
        return text or "(selesai)"

    async def _run_tool(self, ctx: tools.ToolContext, tool_use) -> dict:
        log.info("tool %s %s", tool_use.name, json.dumps(tool_use.input, ensure_ascii=False)[:300])
        try:
            args = tool_use.input if isinstance(tool_use.input, dict) else {}
            output = await asyncio.to_thread(tools.execute, tool_use.name, args, ctx)
            return {"type": "tool_result", "tool_use_id": tool_use.id, "content": output}
        except Exception as e:  # tool gagal: laporkan ke Claude, jangan hentikan percakapan
            log.exception("tool %s gagal", tool_use.name)
            return {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"Error: {type(e).__name__}: {e}",
                "is_error": True,
            }
