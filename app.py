import os, re, json, shutil, subprocess, tempfile
from pathlib import Path
from urllib.parse import urlparse
import gradio as gr

HOOKS = [
    "لكن", "الحقيقة", "المشكلة", "السبب", "تخيل", "اكتشف", "خطأ", "لماذا",
    "because", "but", "secret", "truth", "mistake", "why", "how", "never",
    "important", "surprising", "actually", "problem"
]
MAX_BYTES = 700 * 1024 * 1024
ALLOWED_SCHEMES = {"http", "https"}

def run(cmd, timeout=1800):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or "Command failed")[-3000:])
    return p.stdout.strip()

def validate_url(url):
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        raise gr.Error("أدخل رابطًا صحيحًا يبدأ بـ https:// أو http://")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        raise gr.Error("هذا النوع من الروابط غير مسموح.")
    return url.strip()

def download_video(url, folder, progress):
    # Direct media URL: download only if the remote server exposes a normal public file.
    # Other sites: yt-dlp is used only for public, permitted, non-DRM media.
    direct_ext = Path(urlparse(url).path).suffix.lower()
    dest = folder / "source_video"
    if direct_ext in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        import requests
        try:
            with requests.get(url, stream=True, timeout=(15, 60), allow_redirects=True) as r:
                r.raise_for_status()
                length = int(r.headers.get("content-length", "0") or 0)
                if length and length > MAX_BYTES:
                    raise gr.Error("حجم الفيديو أكبر من الحد التجريبي (700MB).")
                ctype = r.headers.get("content-type", "").lower()
                if "text/html" in ctype:
                    raise gr.Error("الرابط يبدو صفحة ويب وليس ملف فيديو مباشر.")
                total = length or None
                received = 0
                with open(dest.with_suffix(direct_ext), "wb") as f:
                    for chunk in r.iter_content(1024*1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_BYTES:
                            raise gr.Error("الفيديو تجاوز الحد التجريبي (700MB).")
                        f.write(chunk)
                        if total:
                            progress(min(0.22, 0.03 + 0.19*received/total), desc="تنزيل الفيديو من الرابط...")
                return dest.with_suffix(direct_ext)
        except gr.Error:
            raise
        except Exception as e:
            raise gr.Error(f"تعذر تنزيل ملف الفيديو المباشر: {e}")

    # Platform extractor. No cookies, login, DRM bypass, or private-video access.
    try:
        run(["yt-dlp", "--no-playlist", "--no-warnings", "--max-filesize", "700M",
             "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(dest)+".%(ext)s", url], timeout=1800)
    except Exception as e:
        raise gr.Error(
            "لم نتمكن من جلب الفيديو. قد تكون المنصة لا تسمح بالتنزيل، أو الرابط خاصًا، "
            "أو يتطلب تسجيل دخول/صلاحيات. استخدم رابطًا عامًا يسمح مالكه بتنزيله. "
            f"التفاصيل: {str(e)[-700:]}"
        )
    found = list(folder.glob("source_video.*"))
    if not found:
        raise gr.Error("لم يتم العثور على ملف الفيديو بعد التنزيل.")
    return found[0]

def duration_of(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", str(path)]))

def ass_time(sec):
    sec=max(0,float(sec)); h=int(sec//3600); m=int((sec%3600)//60); s=sec%60
    return f"{h}:{m:02d}:{s:05.2f}"

def esc_ass(s):
    return (s or "").replace("\\",r"\\").replace("{",r"\{").replace("}",r"\}")

def write_ass(segments, start, end, path):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,56,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,4,1,2,65,65,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows=[header]
    for seg in segments:
        a,b=max(float(seg.start),start),min(float(seg.end),end)
        text=esc_ass(seg.text.strip())
        if b>a and text:
            rows.append(f"Dialogue: 0,{ass_time(a-start)},{ass_time(b-start)},Default,,0,0,0,,{text}\n")
    path.write_text("".join(rows),encoding="utf-8")

def select_candidates(segments,total,clip_len,count):
    starts=set(); step=max(6,clip_len/5); t=0
    while t<max(1,total-clip_len):
        starts.add(round(t,1)); t+=step
    for s in segments:
        starts.add(round(max(0,min(float(s.start),max(0,total-clip_len))),1))
    pool=[]
    for start in starts:
        end=min(total,start+clip_len)
        active=[s for s in segments if float(s.end)>start and float(s.start)<end]
        transcript=" ".join(s.text.strip() for s in active).strip()
        words=re.findall(r"\w+",transcript,flags=re.UNICODE)
        if len(words)<15: continue
        density=min(len(words)/max(1,end-start),3.5)
        low=transcript.lower()
        hook=sum(1 for w in HOOKS if w in low)
        punct=sum(transcript.count(c) for c in ["؟","?","!",".","،"])
        pool.append({"start":start,"end":end,"score":density*2+min(hook,4)*.7+min(punct,8)*.12,
                     "transcript":transcript})
    pool.sort(key=lambda x:x["score"],reverse=True)
    chosen=[]
    for c in pool:
        if all(c["end"]<=x["start"] or c["start"]>=x["end"] for x in chosen):
            chosen.append(c)
        if len(chosen)>=count: break
    return sorted(chosen,key=lambda x:x["start"])

def filter_path(path):
    return str(path.resolve()).replace("\\","/").replace(":",r"\:").replace("'",r"\'").replace(",",r"\,")

def process_url(url, clip_len, count, model_size, progress=gr.Progress()):
    url=validate_url(url)
    work=Path(tempfile.mkdtemp(prefix="clipforge_url_"))
    try:
        progress(.02,desc="التحقق من الرابط وتجهيز التنزيل...")
        source=download_video(url,work,progress)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise gr.Error("خادم المعالجة غير مجهز بـ FFmpeg.")
        total=duration_of(source)
        if total<12: raise gr.Error("الفيديو أقصر من 12 ثانية.")
        if total>3*60*60: raise gr.Error("الحد الأقصى لمدة الفيديو التجريبية 3 ساعات.")
        progress(.25,desc="تحميل نموذج التعرف على الكلام...")
        from faster_whisper import WhisperModel
        model=WhisperModel(model_size,device="cpu",compute_type="int8")
        progress(.38,desc="تفريغ الكلام واكتشاف اللغة...")
        seg_iter,info=model.transcribe(str(source),beam_size=3,vad_filter=True)
        segments=list(seg_iter)
        if not segments: raise gr.Error("لم يتم العثور على كلام واضح في الفيديو.")
        progress(.52,desc="اختيار المقاطع المرشحة...")
        clips=select_candidates(segments,total,int(clip_len),int(count))
        if not clips: raise gr.Error("لم نعثر على مقاطع كلامية مناسبة.")
        final_dir=Path(tempfile.mkdtemp(prefix="clipforge_results_"))
        files=[]; lines=[]
        for i,c in enumerate(clips,1):
            progress(.52+.46*i/len(clips),desc=f"إنتاج الكليب {i}/{len(clips)}...")
            out=final_dir/f"clip_{i:02d}.mp4"
            ass=work/f"clip_{i:02d}.ass"
            write_ass(segments,c["start"],c["end"],ass)
            vf=f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='{filter_path(ass)}'"
            run(["ffmpeg","-y","-ss",str(c["start"]),"-i",str(source),"-t",str(c["end"]-c["start"]),
                 "-vf",vf,"-c:v","libx264","-preset","ultrafast","-crf","24",
                 "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)],timeout=1800)
            files.append(str(out))
            lines.append(f"Clip {i} | {c['start']:.1f}s–{c['end']:.1f}s\n{c['transcript']}")
        report=final_dir/"clip_report.txt"
        report.write_text(f"Detected language: {getattr(info,'language','unknown')}\n\n"+"\n\n".join(lines),encoding="utf-8")
        return files,str(report),f"تم إنشاء {len(files)} كليب. اللغة المكتشفة: {getattr(info,'language','unknown')}."
    except gr.Error: raise
    except Exception as e: raise gr.Error(f"حدث خطأ أثناء المعالجة: {str(e)[-1200:]}")
    finally:
        shutil.rmtree(work,ignore_errors=True)

with gr.Blocks(theme=gr.themes.Soft(),title="ClipForge AI") as demo:
    gr.Markdown("# 🎬 ClipForge AI\n### الصق رابط فيديو عام ومسموح تنزيله، واحصل على كليبات قصيرة تلقائيًا.")
    with gr.Row():
        with gr.Column(scale=2):
            url=gr.Textbox(label="رابط الفيديو",placeholder="https://...")
            with gr.Row():
                length=gr.Dropdown([20,30,45,60,75,90],value=45,label="مدة الكليب")
                count=gr.Slider(1,8,value=4,step=1,label="عدد الكليبات")
            model=gr.Dropdown(["tiny","base","small"],value="base",label="نموذج اللغة",
                              info="base توازن بين السرعة والدقة.")
            submit=gr.Button("✨ استخرج الكليبات",variant="primary",size="lg")
        with gr.Column(scale=1):
            gr.Markdown("""
            **يدعم:**
            - روابط ملفات الفيديو المباشرة
            - روابط عامة من منصات مدعومة عندما تسمح بالتنزيل
            - العربية والإنجليزية
            - كليبات 9:16 مع كابشن

            **قيود النسخة التجريبية:**
            - لا تدعم الفيديوهات الخاصة أو DRM أو تجاوز تسجيل الدخول
            - حد التنزيل 700MB
            - النتائج مؤقتة على الخادم
            - اختيار اللحظات آلي أولي، وليس تقييمًا مضمونًا للانتشار
            """)
    status=gr.Markdown()
    output=gr.Files(label="الكليبات الجاهزة")
    report=gr.File(label="تقرير التفريغ")
    submit.click(process_url,[url,length,count,model],[output,report,status])
    gr.Markdown("---\nلا تلصق روابط خاصة أو محتوى لا تملك حق تنزيله واستخدامه. أول تشغيل قد يحتاج تنزيل نموذج Whisper.")

if __name__=="__main__":
    demo.queue(max_size=2).launch(server_name="0.0.0.0",server_port=int(os.environ.get("PORT",7860)))
