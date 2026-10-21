"""Tes koneksi ke API AI sebelum menjalankan bot: python test_api.py"""

import asyncio

import anthropic

import config


async def main() -> None:
    if not config.AI_API_KEY:
        raise SystemExit("AI_API_KEY di .env masih kosong.")
    client = anthropic.AsyncAnthropic(base_url=config.AI_BASE_URL, api_key=config.AI_API_KEY)
    print(f"Endpoint: {config.AI_BASE_URL}  |  Model: {config.AI_MODEL}")

    extra = {"output_config": {"effort": config.AI_EFFORT}} if config.AI_EFFORT else {}
    tool = {
        "name": "cek_cuaca",
        "description": "Cek cuaca sebuah kota.",
        "input_schema": {"type": "object", "properties": {"kota": {"type": "string"}}, "required": ["kota"]},
    }

    print("\n1) Pesan biasa ...")
    r = await client.messages.create(
        model=config.AI_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": "Balas singkat: halo, kamu model apa?"}], **extra,
    )
    print("   OK:", " ".join(b.text for b in r.content if b.type == "text")[:200])

    print("\n2) Tool calling ...")
    r = await client.messages.create(
        model=config.AI_MODEL, max_tokens=2000, tools=[tool],
        messages=[{"role": "user", "content": "Gimana cuaca di Bandung? Pakai tool cek_cuaca."}], **extra,
    )
    used = [b for b in r.content if b.type == "tool_use"]
    print("   OK, tool dipanggil:" if used else "   GAGAL: tool tidak dipanggil", [(b.name, b.input) for b in used])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except anthropic.APIStatusError as e:
        print(f"\nERROR {e.status_code}: {e.message}")
        if config.AI_EFFORT and e.status_code == 400:
            print("Coba kosongkan AI_EFFORT di .env, lalu jalankan lagi.")
