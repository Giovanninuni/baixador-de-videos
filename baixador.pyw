"""Baixador - baixa vídeo ou áudio do YouTube, X/Twitter, Instagram e outros.

Usa yt-dlp + ffmpeg. Abra com duplo clique (ou: py baixador.pyw).
"""
import os
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
except ImportError:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Baixador",
        "A biblioteca yt-dlp não está instalada.\n\n"
        "Rode o arquivo atualizar.bat (ou: py -m pip install -U yt-dlp).",
    )
    raise SystemExit(1)

QUALIDADES = {"Melhor": None, "1080p": 1080, "720p": 720, "480p": 480}
NAVEGADORES = ["Nenhum", "chrome", "edge", "firefox", "brave", "opera"]


def pasta_downloads() -> str:
    return str(Path.home() / "Downloads")


def achar_ffmpeg() -> str | None:
    achado = shutil.which("ffmpeg")
    if achado:
        return achado
    # Instalação via winget nem sempre entra no PATH de apps abertos por duplo clique
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in base.glob("*FFmpeg*/**/bin/ffmpeg.exe"):
        return str(exe)
    return None


class Baixador:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ffmpeg = achar_ffmpeg()
        self.baixando = False

        root.title("Baixador")
        root.minsize(560, 420)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        frame = ttk.Frame(root, padding=14)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)

        # Links
        topo = ttk.Frame(frame)
        topo.grid(row=0, column=0, sticky="ew")
        topo.columnconfigure(0, weight=1)
        ttk.Label(topo, text="Link(s) — um por linha:").grid(row=0, column=0, sticky="w")
        ttk.Button(topo, text="Colar", command=self.colar).grid(row=0, column=1)
        ttk.Button(topo, text="Limpar", command=lambda: self.links.delete("1.0", "end")).grid(
            row=0, column=2, padx=(4, 0)
        )

        self.links = tk.Text(frame, height=5, wrap="none", font=("Segoe UI", 10))
        self.links.grid(row=1, column=0, sticky="nsew", pady=(4, 10))
        frame.rowconfigure(1, weight=1)

        # Opções
        opcoes = ttk.Frame(frame)
        opcoes.grid(row=2, column=0, sticky="ew")

        self.tipo = tk.StringVar(value="video")
        ttk.Radiobutton(opcoes, text="Vídeo (MP4)", variable=self.tipo, value="video",
                        command=self.atualizar_opcoes).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(opcoes, text="Só áudio (MP3)", variable=self.tipo, value="audio",
                        command=self.atualizar_opcoes).grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Label(opcoes, text="Qualidade:").grid(row=0, column=2, padx=(24, 4))
        self.qualidade = tk.StringVar(value="Melhor")
        self.combo_qualidade = ttk.Combobox(opcoes, textvariable=self.qualidade, width=8,
                                            values=list(QUALIDADES), state="readonly")
        self.combo_qualidade.grid(row=0, column=3)

        # Pasta
        pasta = ttk.Frame(frame)
        pasta.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        pasta.columnconfigure(1, weight=1)
        ttk.Label(pasta, text="Salvar em:").grid(row=0, column=0)
        self.destino = tk.StringVar(value=pasta_downloads())
        ttk.Entry(pasta, textvariable=self.destino).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(pasta, text="Trocar…", command=self.escolher_pasta).grid(row=0, column=2)

        # Cookies (para Instagram privado/stories etc.)
        cookies = ttk.Frame(frame)
        cookies.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(cookies, text="Usar login do navegador:").grid(row=0, column=0)
        self.navegador = tk.StringVar(value="Nenhum")
        ttk.Combobox(cookies, textvariable=self.navegador, width=9, values=NAVEGADORES,
                     state="readonly").grid(row=0, column=1, padx=4)
        ttk.Label(cookies, text="(só se precisar: posts privados, stories)",
                  foreground="gray").grid(row=0, column=2)

        # Ações
        acoes = ttk.Frame(frame)
        acoes.grid(row=5, column=0, sticky="ew", pady=(14, 6))
        acoes.columnconfigure(0, weight=1)
        self.botao = ttk.Button(acoes, text="Baixar", command=self.iniciar)
        self.botao.grid(row=0, column=0, sticky="ew", ipady=4)
        ttk.Button(acoes, text="Abrir pasta", command=self.abrir_pasta).grid(row=0, column=1, padx=(6, 0))

        self.progresso = ttk.Progressbar(frame, maximum=100)
        self.progresso.grid(row=6, column=0, sticky="ew")
        self.status = tk.StringVar(value="Pronto." if self.ffmpeg else
                                   "Atenção: ffmpeg não encontrado — MP3 e juntar vídeo+áudio não vão funcionar.")
        ttk.Label(frame, textvariable=self.status, wraplength=520).grid(row=7, column=0, sticky="w", pady=(6, 0))

        self.links.focus_set()

    # --- UI ---
    def atualizar_opcoes(self):
        self.combo_qualidade.configure(state="readonly" if self.tipo.get() == "video" else "disabled")

    def colar(self):
        try:
            texto = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        atual = self.links.get("1.0", "end").strip()
        if atual:
            self.links.insert("end", "\n")
        self.links.insert("end", texto)

    def escolher_pasta(self):
        escolhida = filedialog.askdirectory(initialdir=self.destino.get())
        if escolhida:
            self.destino.set(escolhida)

    def abrir_pasta(self):
        pasta = self.destino.get()
        if os.path.isdir(pasta):
            os.startfile(pasta)

    def set_status(self, texto: str, pct: float | None = None):
        def aplicar():
            self.status.set(texto)
            if pct is not None:
                self.progresso["value"] = pct
        self.root.after(0, aplicar)

    # --- Download ---
    def iniciar(self):
        if self.baixando:
            return
        urls = [u.strip() for u in self.links.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showinfo("Baixador", "Cole pelo menos um link.")
            return
        destino = self.destino.get()
        os.makedirs(destino, exist_ok=True)

        self.baixando = True
        self.botao.configure(state="disabled", text="Baixando…")
        threading.Thread(target=self.baixar_todos, args=(urls, destino), daemon=True).start()

    def opcoes_ytdlp(self, destino: str) -> dict:
        opts = {
            "outtmpl": os.path.join(destino, "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "windowsfilenames": True,
            "progress_hooks": [self.hook_progresso],
            "postprocessor_hooks": [self.hook_pos],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        if self.ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg
        if self.navegador.get() != "Nenhum":
            opts["cookiesfrombrowser"] = (self.navegador.get(),)

        if self.tipo.get() == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            h = QUALIDADES[self.qualidade.get()]
            filtro = f"[height<={h}]" if h else ""
            # Prefere H.264/AAC (toca em qualquer player); cai para o melhor disponível
            opts["format"] = (
                f"bv*{filtro}[vcodec^=avc1]+ba[acodec^=mp4a]/"
                f"bv*{filtro}+ba/b{filtro}/b"
            )
            opts["merge_output_format"] = "mp4"
        return opts

    def baixar_todos(self, urls: list[str], destino: str):
        ok, falhas = 0, []
        for i, url in enumerate(urls, 1):
            self.prefixo = f"[{i}/{len(urls)}] " if len(urls) > 1 else ""
            self.set_status(f"{self.prefixo}Buscando informações…", 0)
            try:
                with yt_dlp.YoutubeDL(self.opcoes_ytdlp(destino)) as ydl:
                    ydl.download([url])
                ok += 1
            except Exception as e:  # yt-dlp lança DownloadError e afins
                falhas.append((url, self.erro_amigavel(str(e))))
        self.root.after(0, self.terminar, ok, falhas)

    def hook_progresso(self, d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            baixado = d.get("downloaded_bytes") or 0
            pct = baixado / total * 100 if total else 0
            vel = d.get("speed") or 0
            eta = d.get("eta")
            partes = [f"{pct:.0f}%" if total else f"{baixado / 1e6:.1f} MB"]
            if vel:
                partes.append(f"{vel / 1e6:.1f} MB/s")
            if eta:
                partes.append(f"faltam {int(eta)}s")
            self.set_status(f"{self.prefixo}Baixando… " + " · ".join(partes), pct)
        elif d["status"] == "finished":
            self.set_status(f"{self.prefixo}Download concluído, processando…", 100)

    def hook_pos(self, d: dict):
        if d["status"] == "started":
            nomes = {"FFmpegExtractAudio": "Convertendo para MP3…",
                     "Merger": "Juntando vídeo e áudio…"}
            self.set_status(self.prefixo + nomes.get(d.get("postprocessor"), "Processando…"))

    @staticmethod
    def erro_amigavel(msg: str) -> str:
        m = msg.lower()
        if "unsupported url" in m:
            return "link não suportado"
        if "private" in m or "login" in m or "cookies" in m:
            return "conteúdo privado/precisa de login — tente 'Usar login do navegador'"
        if "unavailable" in m:
            return "vídeo indisponível"
        if "ffmpeg" in m:
            return "ffmpeg não encontrado"
        return msg.replace("ERROR: ", "").strip()[:200]

    def terminar(self, ok: int, falhas: list):
        self.baixando = False
        self.botao.configure(state="normal", text="Baixar")
        if not falhas:
            self.status.set(f"Pronto! {ok} arquivo(s) salvo(s) em {self.destino.get()}")
            self.progresso["value"] = 100
            self.links.delete("1.0", "end")
        else:
            self.status.set(f"{ok} ok, {len(falhas)} com erro.")
            detalhes = "\n\n".join(f"{u}\n→ {e}" for u, e in falhas)
            messagebox.showerror("Baixador", f"Não consegui baixar:\n\n{detalhes}\n\n"
                                 "Se um site parou de funcionar, rode atualizar.bat.")


def main():
    # Faz a barra de tarefas mostrar o ícone do app em vez do ícone do Python
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("baixador.videos")
    except Exception:
        pass
    root = tk.Tk()
    icone = Path(__file__).with_name("icone.ico")
    if icone.exists():
        root.iconbitmap(default=str(icone))
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    Baixador(root)
    root.mainloop()


if __name__ == "__main__":
    main()
