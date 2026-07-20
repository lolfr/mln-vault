#!/usr/bin/env python3
"""hub_transcribe.py — transcription bulk via le hub mlx (whisper-large-v3), reprenable/idempotent.
Saute les vidéos qui ont déjà un transcript. Token lu depuis api_keys.yaml (jamais hardcodé).
Les vidéos SANS piste audio (muettes) sont marquées `[no audio]` (pré-check ffprobe) pour ne pas
être re-tentées en boucle chaque nuit.
Usage: python hub_transcribe.py [LIMIT]     (LIMIT=0 → tout)
       SILENT_ONLY=1 python hub_transcribe.py   → marque seulement les muettes, ne transcrit pas.
"""
import yaml, sqlite3, os, sys, json, subprocess
import httpx
ROOT=os.environ.get("ARCHIVE_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); DB=ROOT+"/metadata/catalog.sqlite"
HUB="http://127.0.0.1:8765/transcribe"
KEYF="/Users/mini/mln-homelab/mln-inference-hub/config/api_keys.yaml"
FFPROBE=os.environ.get("FFPROBE","/opt/homebrew/bin/ffprobe")
SILENT_ONLY=os.environ.get("SILENT_ONLY")=="1"
limit=int(sys.argv[1]) if len(sys.argv)>1 else 0
tok=next(c["token"] for c in yaml.safe_load(open(KEYF))["clients"] if "transcribe" in c.get("allowed_endpoints",[]))
con=sqlite3.connect(DB, timeout=120); con.row_factory=sqlite3.Row

def has_audio(path):
    """True si la vidéo a au moins une piste audio ; False si muette ; None si ffprobe KO."""
    try:
        out=subprocess.run([FFPROBE,"-v","error","-select_streams","a","-show_entries","stream=index",
                            "-of","csv=p=0",path], capture_output=True, text=True, timeout=60).stdout.strip()
        return bool(out)
    except Exception:
        return None

def media_duration_s(path):
    """Duree de la video en secondes, ou None si ffprobe echoue."""
    try:
        out=subprocess.run([FFPROBE,"-v","error","-show_entries","format=duration",
                            "-of","csv=p=0",path], capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return None


def http_timeout_for(path):
    """Timeout proportionnel : whisper tourne a ~0.5x temps reel (mesure 20/07/2026,
    8409 s de video -> 4157 s de traitement). 1.5x = 3x de marge. Plancher 900 s.
    ffprobe KO -> 3600 s (large, mais borne : on ne veut pas d'attente infinie)."""
    d = media_duration_s(path)
    if d is None:
        return 3600.0
    return max(900.0, d * 1.5)


def mark_silent(item_id):
    con.execute("""INSERT OR REPLACE INTO transcripts(item_id,lang,text,segments_json,model,word_count,created_at,updated_at)
        VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (item_id, None, "[no audio]", None, "no-audio", 0))
    con.commit()

rows=con.execute("""SELECT id, file_path_master FROM items
  WHERE item_type='video' AND file_path_master IS NOT NULL AND file_path_master<>''
  AND id NOT IN (SELECT item_id FROM transcripts) ORDER BY id""").fetchall()
if limit: rows=rows[:limit]
print(f"à traiter: {len(rows)}"+(" (SILENT_ONLY)" if SILENT_ONLY else ""),flush=True)
ok=err=silent=0
for i,r in enumerate(rows,1):
    vid=os.path.join(ROOT, r["file_path_master"])
    if not os.path.exists(vid): print("  manquant:",r["id"],flush=True); err+=1; continue
    aud=has_audio(vid)
    if aud is False:                                  # muette → marquer, ne pas transcrire
        mark_silent(r["id"]); silent+=1
        if silent%25==0: print(f"  …muettes marquées={silent}",flush=True)
        continue
    if SILENT_ONLY:                                   # mode marquage seul : on saute les vidéos avec audio
        continue
    try:
        with open(vid,"rb") as f:
            resp=httpx.post(HUB, headers={"X-API-Key":tok}, files={"audio":(os.path.basename(vid),f)}, data={"cache":"false"}, timeout=http_timeout_for(vid))
        if resp.status_code!=200: print("  HTTP",resp.status_code,r["id"],resp.text[:120],flush=True); err+=1; continue
        d=resp.json(); text=d.get("text") or ""; segs=d.get("segments")
        con.execute("""INSERT OR REPLACE INTO transcripts(item_id,lang,text,segments_json,model,word_count,created_at,updated_at)
            VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (r["id"], d.get("lang"), text, json.dumps(segs,ensure_ascii=False) if segs else None,
             "hub:whisper-large-v3-mlx", len(text.split())))
        con.commit(); ok+=1
        if i%25==0 or limit: print(f"  …{i}/{len(rows)} ok={ok} silent={silent} err={err} (last {r['id']} {d.get('lang')} {len(text.split())}w {d.get('processing_seconds')}s)",flush=True)
    except Exception as e:
        print("  EXC",r["id"],str(e)[:140],flush=True); err+=1
print(f"DONE ok={ok} silent={silent} err={err} · transcripts total={con.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0]}",flush=True)

# Sortie non nulle quand RIEN n'a abouti alors que des items ont echoue :
# c'est la signature d'un backend mort, pas d'un backlog traite. transcribe_fda.sh
# logue alors `END rc=1`, que feed_state (mln-phase-dispatch.sh) lit comme
# `resume` -> le feed repart au tick suivant, une fois le hub relance.
# On n'echoue PAS des que err>0 : avec ok>0, un seul fichier corrompu ferait
# boucler la reprise toute la nuit sur cet item.
if ok == 0 and err > 0:
    sys.exit(1)

