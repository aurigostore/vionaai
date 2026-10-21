# AI Agent Pribadi (Telegram)

Asisten pribadi berbasis Claude yang berjalan di Telegram:

- **Memori jangka panjang**: mengingat fakta tentang kamu secara otomatis (`/memori`, `/lupa <id>`)
- **Kesehatan**: profil fisik; perhitungan BMI, kalori, protein, dan air; pencatatan minum, makan, berat, tidur, dan latihan gym; laporan progres (`/profil`)
- **Pengingat**: sekali jalan atau harian, dan tetap aktif walau bot di-restart (`/pengingat`)
- **Internet**: mencari dan membaca halaman web
- **File**: membaca foto, PDF, dan file teks
- **Privat**: hanya membalas ID Telegram yang ada di `ALLOWED_USER_IDS`

## Struktur

| File | Isi |
|---|---|
| `bot.py` | Entry point Telegram: handler, perintah, penjadwal pengingat |
| `agent.py` | Persona, penyusunan konteks (memori + profil), loop tool Claude |
| `tools.py` | Daftar tool dan eksekusinya |
| `health.py` | Rumus kesehatan (Mifflin-St Jeor, BMI Asia, protein, air) |
| `db.py` | Database SQLite (`data/agent.db`) |
| `config.py` | Membaca `.env` |
| `test_api.py` | Tes koneksi ke API AI |

## Jalankan di komputer (Windows)

1. Isi `.env`: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `AI_API_KEY`
2. Tes API:
   ```
   .venv\Scripts\python test_api.py
   ```
3. Jalankan bot:
   ```
   .venv\Scripts\python bot.py
   ```

Catatan: **jangan jalankan bot dengan token yang sama di dua tempat sekaligus**, misalnya di laptop dan VPS. Telegram akan menolak salah satunya (error `Conflict`).

## Deploy ke VPS (Ubuntu/Debian)

```bash
# 1. MATIKAN bot di PC dulu (Ctrl+C), lalu upload dari Windows (tanpa .venv).
#    Folder data/ berisi semua ingatan bot, jadi wajib ikut.
ssh user@IP_VPS "mkdir -p /tmp/ai-agent"
scp -r config.py db.py health.py tools.py agent.py bot.py test_api.py requirements.txt .env deploy data user@IP_VPS:/tmp/ai-agent/

# 2. Di VPS
sudo mkdir -p /opt/ai-agent && sudo cp -r /tmp/ai-agent/* /opt/ai-agent/
sudo chown -R $USER:$USER /opt/ai-agent && cd /opt/ai-agent
sudo apt install -y python3-venv sqlite3
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod 600 .env
.venv/bin/python test_api.py

# 3. Pasang sebagai service (ganti GANTI_USER dengan user VPS kamu)
sed -i "s/GANTI_USER/$USER/" deploy/ai-agent.service
sudo cp deploy/ai-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-agent

# 4. Cek
sudo systemctl status ai-agent
journalctl -u ai-agent -f

# 5. Backup harian (opsional)
chmod +x deploy/backup.sh
(crontab -l; echo "15 3 * * * /opt/ai-agent/deploy/backup.sh") | crontab -
```

Bot memakai mode polling, jadi tidak perlu domain atau membuka port. Bot juga punya virtualenv dan service sendiri, sehingga tidak mengganggu bot lain di VPS.

Update kode: upload ulang file `.py`, lalu `sudo systemctl restart ai-agent`.

## Pengaturan `.env`

| Variabel | Keterangan |
|---|---|
| `AI_MODEL` | Default `claude-opus-5-5`. Bisa diganti sesuai model yang tersedia di provider |
| `AI_EFFORT` | `low`/`medium`/`high`. Kosongkan kalau provider menolak parameter ini |
| `HISTORY_LIMIT` | Jumlah pesan terakhir yang dibawa ke setiap percakapan (makin besar, makin boros token) |
| `TIMEZONE` | Default `Asia/Jakarta` |
