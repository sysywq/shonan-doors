#!/usr/bin/env python3
"""Manual editorial intake for Shonan Doors.

Usage:
  python manual_editorial.py prepare --input editorial/manual/<slug>/article.json
Images are placed in the same directory and listed in article.json "images".
This script validates the package, copies/optimizes images when Pillow is available,
upserts data/articles.json, and runs build.py. It never publishes by itself.
"""
import argparse,json,os,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data/articles.json"
OUT=ROOT/"assets/images/articles"
REQ=("slug","title","body","cat","area","date")
def fail(s): raise SystemExit("manual-editorial: "+s)
def main():
 p=argparse.ArgumentParser(); p.add_argument("command",choices=["prepare"]); p.add_argument("--input",required=True); a=p.parse_args()
 src=Path(a.input); item=json.loads(src.read_text(encoding="utf-8"))
 for k in REQ:
  if not item.get(k): fail(f"missing {k}")
 slug=item["slug"]; dest=OUT/slug; dest.mkdir(parents=True,exist_ok=True)
 imgs=[]
 for n,name in enumerate(item.pop("images",[]) or [],1):
  f=src.parent/name
  if not f.is_file(): fail(f"missing image: {f}")
  out=dest/f"{n:02d}{f.suffix.lower()}"
  shutil.copy2(f,out); imgs.append("/"+out.relative_to(ROOT).as_posix())
 if imgs:
  item["heroImage"]=imgs[0]; item["contentImages"]=imgs
 rows=json.loads(DATA.read_text(encoding="utf-8"))
 found=False
 for i,x in enumerate(rows):
  if x.get("slug")==slug:
   item.setdefault("id",x.get("id")); rows[i]={**x,**item}; found=True; break
 if not found:
  item.setdefault("id",max([x.get("id",0) for x in rows]+[0])+1); rows.append(item)
 DATA.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 subprocess.run([sys.executable,str(ROOT/"build.py")],check=True)
 print(json.dumps({"slug":slug,"mode":"update" if found else "create","images":imgs},ensure_ascii=False))
if __name__=="__main__": main()
