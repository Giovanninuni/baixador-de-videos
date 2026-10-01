"""Aba "Cortar áudio": abre um arquivo, mostra a onda, escolhe início/fim e salva o trecho.

Usa ffmpeg (decodificar/salvar) e ffplay (ouvir a prévia), que vêm juntos.
"""
import json
import os
import subprocess
import threading
import time
from array import array
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAXA_ONDA = 4000      # amostras/s usadas só para desenhar a onda
N_PICOS = 1600        # resolução da onda
DIST_BORDA = 8        # px: clicar mais perto que isso de uma borda arrasta a borda
TIPOS_AUDIO = [
    ("Áudio e vídeo", "*.mp3 *.m4a *.wav *.ogg *.opus *.flac *.aac *.wma *.mp4 *.mkv *.webm *.mov"),
    ("Todos os arquivos", "*.*"),
]
TIPOS_SAIDA = [("MP3", "*.mp3"), ("M4A", "*.m4a"), ("WAV", "*.wav"), ("OGG", "*.ogg"), ("FLAC", "*.flac")]
CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
}

# Volume final do arquivo, em LUFS (decibéis de volume percebido; é a medida usada por YouTube/Spotify)
VOLUMES = {
    "Original (não mexer)": None,
    "-23 LUFS · baixo (padrão de TV)": -23,
    "-18 LUFS · médio": -18,
    "-14 LUFS · normal (YouTube/Spotify)": -14,
    "-11 LUFS · alto": -11,
    "-8 LUFS · muito alto": -8,
}
LIMITE_DB = -1.5  # o limitador segura os picos aqui, para o volume alto não distorcer

COR_FUNDO = "#1e1e2e"
COR_ONDA = "#6c6f85"
COR_ONDA_SEL = "#e64980"
COR_SEL = "#3a2a3f"
COR_MARCADOR = "#ffffff"
COR_CURSOR = "#ffd43b"

AJUDA = ("Clique na onda para mover o cursor amarelo · ←/→ anda 1 s (Shift 5 s, Ctrl 0,1 s) · "
         "Espaço toca/pausa · I / O marcam início / fim no cursor · arraste as barras brancas para ajustar o corte")


def filtro_ganho(ganho_db: float) -> str:
    """Ganho fixo + limitador. Para baixar o volume o limitador nem age; para subir muito, segura os picos."""
    limite = 10 ** (LIMITE_DB / 20)
    return f"volume={ganho_db:.2f}dB,alimiter=limit={limite:.4f}:level=false:attack=5:release=80"


def ler_json_loudnorm(stderr: str) -> dict | None:
    try:
        return json.loads(stderr[stderr.rindex("{"): stderr.rindex("}") + 1])
    except ValueError:
        return None


def fmt_tempo(seg: float) -> str:
    m, s = divmod(max(seg, 0), 60)
    return f"{int(m)}:{s:06.3f}"


def ler_tempo(texto: str) -> float:
    """Aceita '83.5', '1:23.5' ou '0:01:23.5'."""
    partes = texto.strip().replace(",", ".").split(":")
    total = 0.0
    for p in partes:
        total = total * 60 + float(p)
    return total


class EditorAudio:
    def __init__(self, root: tk.Tk, parent, ffmpeg: str | None):
        self.root = root
        self.ffmpeg = ffmpeg
        pasta = Path(ffmpeg).parent if ffmpeg else None
        self.ffprobe = str(pasta / "ffprobe.exe") if pasta and (pasta / "ffprobe.exe").exists() else None
        self.ffplay = str(pasta / "ffplay.exe") if pasta and (pasta / "ffplay.exe").exists() else None

        self.arquivo: str | None = None
        self.duracao = 0.0
        self.taxa = 44100
        self.lufs_arquivo: float | None = None
        self.picos: list[float] = []
        self.inicio = 0.0
        self.fim = 0.0
        self.cursor = 0.0
        self.arrastando: str | None = None   # "inicio", "fim" ou "cursor"
        self.player: subprocess.Popen | None = None
        self.tocando_desde = 0.0
        self.tocando_de = 0.0
        self.tocando_ate = 0.0

        frame = ttk.Frame(parent, padding=14)
        frame.grid(sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        topo = ttk.Frame(frame)
        topo.grid(row=0, column=0, sticky="ew")
        topo.columnconfigure(1, weight=1)
        ttk.Button(topo, text="Abrir arquivo…", command=self.abrir).grid(row=0, column=0)
        self.nome = tk.StringVar(value="Nenhum arquivo aberto.")
        ttk.Label(topo, textvariable=self.nome).grid(row=0, column=1, sticky="w", padx=8)
        self.txt_cursor = tk.StringVar(value="")
        ttk.Label(topo, textvariable=self.txt_cursor, font=("Consolas", 11)).grid(row=0, column=2)

        self.canvas = tk.Canvas(frame, height=150, bg=COR_FUNDO, highlightthickness=0, takefocus=1)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=10)
        frame.rowconfigure(1, weight=1)
        self.canvas.bind("<Configure>", lambda e: self.desenhar())
        self.canvas.bind("<ButtonPress-1>", self.clicar)
        self.canvas.bind("<B1-Motion>", self.arrastar)
        self.canvas.bind("<ButtonRelease-1>", self.soltar)
        self.canvas.bind("<Motion>", self.trocar_ponteiro)
        ttk.Label(frame, text=AJUDA, foreground="gray", wraplength=540).grid(row=2, column=0, sticky="w")

        # Atalhos valem na aba inteira, menos quando se está digitando num campo de texto
        for tecla, acao in {
            "<Left>": lambda e: self.andar(-1), "<Right>": lambda e: self.andar(1),
            "<Shift-Left>": lambda e: self.andar(-5), "<Shift-Right>": lambda e: self.andar(5),
            "<Control-Left>": lambda e: self.andar(-0.1), "<Control-Right>": lambda e: self.andar(0.1),
            "<space>": lambda e: self.tocar_pausar(),
            "<Home>": lambda e: self.ir_para(self.inicio), "<End>": lambda e: self.ir_para(self.fim),
            "<i>": lambda e: self.marcar_inicio(), "<I>": lambda e: self.marcar_inicio(),
            "<o>": lambda e: self.marcar_fim(), "<O>": lambda e: self.marcar_fim(),
        }.items():
            root.bind(tecla, self.atalho(acao), add="+")

        tempos = ttk.Frame(frame)
        tempos.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.txt_inicio = tk.StringVar(value=fmt_tempo(0))
        self.txt_fim = tk.StringVar(value=fmt_tempo(0))
        self.txt_dur = tk.StringVar(value="")
        ttk.Label(tempos, text="Início:").grid(row=0, column=0)
        e1 = ttk.Entry(tempos, textvariable=self.txt_inicio, width=11)
        e1.grid(row=0, column=1, padx=(4, 2))
        ttk.Button(tempos, text="← cursor", width=8, command=self.marcar_inicio).grid(row=0, column=2, padx=(0, 16))
        ttk.Label(tempos, text="Fim:").grid(row=0, column=3)
        e2 = ttk.Entry(tempos, textvariable=self.txt_fim, width=11)
        e2.grid(row=0, column=4, padx=(4, 2))
        ttk.Button(tempos, text="← cursor", width=8, command=self.marcar_fim).grid(row=0, column=5, padx=(0, 16))
        ttk.Label(tempos, textvariable=self.txt_dur).grid(row=0, column=6)
        for e in (e1, e2):
            e.bind("<Return>", self.tempos_digitados)
            e.bind("<FocusOut>", self.tempos_digitados)

        efeitos = ttk.Frame(frame)
        efeitos.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(efeitos, text="Volume final:").grid(row=0, column=0)
        self.volume = tk.StringVar(value=next(iter(VOLUMES)))
        ttk.Combobox(efeitos, textvariable=self.volume, values=list(VOLUMES), state="readonly",
                     width=34).grid(row=0, column=1, padx=(4, 10))
        self.txt_volume_atual = tk.StringVar(value="")
        ttk.Label(efeitos, textvariable=self.txt_volume_atual, foreground="gray").grid(row=0, column=2)
        # Trocou o volume com a prévia tocando: recomeça dali já com o volume novo
        self.volume.trace_add("write", lambda *_: self.tocando() and self.tocar(self.posicao_atual(),
                                                                                self.tocando_ate))
        self.fade = tk.BooleanVar(value=False)
        ttk.Checkbutton(efeitos, text="Suavizar começo e fim (fade de 0,5 s)", variable=self.fade).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        acoes = ttk.Frame(frame)
        acoes.grid(row=5, column=0, sticky="ew", pady=(14, 6))
        acoes.columnconfigure(3, weight=1)
        self.botao_tocar = ttk.Button(acoes, text="▶ Tocar", width=10, command=self.tocar_pausar)
        self.botao_tocar.grid(row=0, column=0)
        ttk.Button(acoes, text="▶ Ouvir trecho", command=self.ouvir).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(acoes, text="■ Parar", command=self.parar).grid(row=0, column=2, padx=(6, 0))
        self.botao_salvar = ttk.Button(acoes, text="Salvar corte…", command=self.salvar)
        self.botao_salvar.grid(row=0, column=3, sticky="ew", padx=(12, 0), ipady=4)

        self.status = tk.StringVar(value="Abra um áudio (ou vídeo) para começar." if ffmpeg
                                   else "ffmpeg não encontrado — o editor não vai funcionar.")
        ttk.Label(frame, textvariable=self.status, wraplength=540).grid(row=6, column=0, sticky="w", pady=(6, 0))

    # --- Abrir ---
    def abrir(self, caminho: str | None = None):
        if not self.ffmpeg or not self.ffprobe:
            messagebox.showerror("Cortar áudio", "ffmpeg/ffprobe não encontrados.")
            return
        caminho = caminho or filedialog.askopenfilename(filetypes=TIPOS_AUDIO,
                                                        initialdir=str(Path.home() / "Downloads"))
        if not caminho:
            return
        self.parar()
        self.status.set("Lendo áudio…")
        self.nome.set(Path(caminho).name)
        threading.Thread(target=self.carregar, args=(caminho,), daemon=True).start()

    def carregar(self, caminho: str):
        try:
            info = json.loads(subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "format=duration:stream=sample_rate", "-of", "json", caminho],
                capture_output=True, text=True, creationflags=SEM_JANELA, check=True).stdout)
            duracao = float(info["format"]["duration"])
            taxa = int((info.get("streams") or [{}])[0].get("sample_rate") or 44100)
            bruto = subprocess.run(
                [self.ffmpeg, "-v", "error", "-i", caminho, "-vn", "-ac", "1", "-ar", str(TAXA_ONDA),
                 "-f", "s16le", "-"],
                capture_output=True, creationflags=SEM_JANELA, check=True).stdout
        except (subprocess.CalledProcessError, ValueError, KeyError):
            self.root.after(0, self.falha_ao_abrir)
            return

        amostras = array("h")
        amostras.frombytes(bruto[: len(bruto) // 2 * 2])
        picos = []
        if amostras:
            passo = max(1, len(amostras) // N_PICOS)
            for i in range(0, len(amostras), passo):
                bloco = amostras[i:i + passo]
                picos.append(max(max(bloco), -min(bloco)))
            topo = max(picos) or 1
            picos = [p / topo for p in picos]
        self.root.after(0, self.carregado, caminho, duracao, taxa, picos)
        lufs = self.medir_volume(caminho, 0, duracao)

        def mostrar():
            if self.arquivo == caminho:
                self.lufs_arquivo = lufs
                self.txt_volume_atual.set(f"(o arquivo está em {lufs:.1f} LUFS)" if lufs is not None else "")
        self.root.after(0, mostrar)

    def medir_volume(self, caminho: str, ini: float, dur: float, filtro: str | None = None) -> float | None:
        """Volume (LUFS) do trecho, opcionalmente depois de passar por `filtro`. None se for silêncio."""
        medidor = "loudnorm=print_format=json"
        r = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostats", "-ss", f"{ini:.3f}", "-i", caminho, "-t", f"{dur:.3f}",
             "-vn", "-af", f"{filtro},{medidor}" if filtro else medidor, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=SEM_JANELA)
        medido = ler_json_loudnorm(r.stderr)
        try:
            lufs = float(medido["input_i"])
        except (TypeError, KeyError, ValueError):
            return None
        return lufs if lufs > -70 else None

    def calcular_ganho(self, arquivo: str, ini: float, dur: float, alvo: float):
        """Acha o ganho que deixa o trecho em `alvo` LUFS: aplica, mede o resultado e corrige.
        Volta (ganho, volume_original, volume_final) ou None se for silêncio."""
        original = self.medir_volume(arquivo, ini, dur)
        if original is None:
            return None
        ganho, final = alvo - original, original
        for _ in range(4):
            final = self.medir_volume(arquivo, ini, dur, filtro_ganho(ganho))
            if final is None or abs(alvo - final) < 0.2:
                break
            ganho += alvo - final
        return ganho, original, final

    def falha_ao_abrir(self):
        self.nome.set("Nenhum arquivo aberto.")
        self.status.set("Não consegui ler esse arquivo (tem áudio?).")

    def carregado(self, caminho: str, duracao: float, taxa: int, picos: list[float]):
        self.arquivo, self.duracao, self.taxa, self.picos = caminho, duracao, taxa, picos
        self.lufs_arquivo = None
        self.txt_volume_atual.set("(medindo volume…)")
        self.inicio, self.fim, self.cursor = 0.0, duracao, 0.0
        self.atualizar_tempos()
        self.status.set(f"Duração total: {fmt_tempo(duracao)}")
        self.canvas.focus_set()

    # --- Onda, bordas e cursor ---
    def x_para_t(self, x: float) -> float:
        w = max(self.canvas.winfo_width(), 1)
        return min(max(x / w, 0), 1) * self.duracao

    def t_para_x(self, t: float) -> float:
        return t / self.duracao * self.canvas.winfo_width() if self.duracao else 0

    def desenhar(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not self.picos:
            return
        x1, x2 = self.t_para_x(self.inicio), self.t_para_x(self.fim)
        c.create_rectangle(x1, 0, x2, h, fill=COR_SEL, width=0)
        meio, n = h / 2, len(self.picos)
        for x in range(w):
            a, b = x * n // w, max((x + 1) * n // w, x * n // w + 1)
            p = max(self.picos[a:b]) * (meio - 6)
            c.create_line(x, meio - p, x, meio + p + 1, fill=COR_ONDA_SEL if x1 <= x <= x2 else COR_ONDA)
        for x in (x1, x2):
            c.create_line(x, 0, x, h, fill=COR_MARCADOR, width=2)
            c.create_rectangle(x - 5, 0, x + 5, 12, fill=COR_MARCADOR, width=0)
        c.create_line(0, 0, 0, h, fill=COR_CURSOR, width=2, tags="cursor")
        c.create_polygon(-6, h, 6, h, 0, h - 9, fill=COR_CURSOR, tags="cursor")
        self.mover_cursor()

    def mover_cursor(self):
        """Só reposiciona a linha amarela (barato, chamado a cada quadro da reprodução)."""
        self.txt_cursor.set(fmt_tempo(self.cursor) if self.arquivo else "")
        c = self.canvas
        itens = c.find_withtag("cursor")
        if not itens:
            return
        h, cx = c.winfo_height(), self.t_para_x(self.cursor)
        c.coords(itens[0], cx, 0, cx, h)
        c.coords(itens[1], cx - 6, h, cx + 6, h, cx, h - 9)

    def borda_perto(self, x: float) -> str | None:
        d1, d2 = abs(x - self.t_para_x(self.inicio)), abs(x - self.t_para_x(self.fim))
        if min(d1, d2) > DIST_BORDA:
            return None
        return "inicio" if d1 <= d2 else "fim"

    def trocar_ponteiro(self, e):
        self.canvas.configure(cursor="sb_h_double_arrow" if self.picos and self.borda_perto(e.x) else "hand2")

    def clicar(self, e):
        self.canvas.focus_set()
        if not self.picos:
            return
        self.arrastando = self.borda_perto(e.x) or "cursor"
        self.arrastar(e)

    def arrastar(self, e):
        if not self.arrastando:
            return
        t = self.x_para_t(e.x)
        if self.arrastando == "inicio":
            self.inicio = min(t, self.fim - 0.05)
            self.atualizar_tempos()
        elif self.arrastando == "fim":
            self.fim = max(t, self.inicio + 0.05)
            self.atualizar_tempos()
        else:
            self.cursor = t
            self.mover_cursor()

    def soltar(self, _e):
        # Soltou o cursor durante a reprodução: continua tocando da nova posição
        if self.arrastando == "cursor" and self.tocando():
            self.tocar(self.cursor, self.duracao)
        self.arrastando = None

    # --- Navegação pelo teclado ---
    def atalho(self, acao):
        def handler(e):
            if not self.arquivo or not self.canvas.winfo_viewable():
                return None
            classe = e.widget.winfo_class()
            # Campos de texto usam as setas; botões já tratam o Espaço sozinhos
            if classe in ("Entry", "TEntry", "Text", "TCombobox"):
                return None
            if e.keysym == "space" and classe in ("TButton", "TCheckbutton", "TRadiobutton"):
                return None
            acao(e)
            return "break"
        return handler

    def ir_para(self, t: float):
        self.cursor = min(max(t, 0.0), self.duracao)
        self.mover_cursor()
        if self.tocando():
            self.tocar(self.cursor, self.duracao)

    def andar(self, seg: float):
        self.ir_para(self.posicao_atual() + seg)

    def marcar_inicio(self):
        if self.arquivo:
            self.inicio = min(self.posicao_atual(), self.fim - 0.05)
            self.atualizar_tempos()

    def marcar_fim(self):
        if self.arquivo:
            self.fim = max(self.posicao_atual(), self.inicio + 0.05)
            self.atualizar_tempos()

    def tempos_digitados(self, _e=None):
        if not self.arquivo:
            return
        try:
            ini, fim = ler_tempo(self.txt_inicio.get()), ler_tempo(self.txt_fim.get())
        except ValueError:
            self.atualizar_tempos()
            return
        ini, fim = max(0.0, ini), min(self.duracao, fim)
        if fim - ini >= 0.05:
            self.inicio, self.fim = ini, fim
        self.atualizar_tempos()

    def atualizar_tempos(self):
        self.inicio = max(0.0, self.inicio)
        self.fim = min(self.duracao, self.fim)
        self.txt_inicio.set(fmt_tempo(self.inicio))
        self.txt_fim.set(fmt_tempo(self.fim))
        self.txt_dur.set(f"Trecho: {fmt_tempo(self.fim - self.inicio)}")
        self.desenhar()

    # --- Reprodução ---
    def tocando(self) -> bool:
        return bool(self.player and self.player.poll() is None)

    def posicao_atual(self) -> float:
        if self.tocando():
            return min(self.tocando_de + time.monotonic() - self.tocando_desde, self.tocando_ate)
        return self.cursor

    def tocar(self, de: float, ate: float):
        if not self.arquivo:
            return
        if not self.ffplay:
            messagebox.showerror("Cortar áudio", "ffplay não encontrado.")
            return
        self.parar(manter_posicao=False)
        if ate - de < 0.05:
            de = 0.0  # cursor no final: recomeça do início
        cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-ss", f"{de:.3f}", "-t", f"{ate - de:.3f}"]
        alvo = VOLUMES[self.volume.get()]
        if alvo is not None and self.lufs_arquivo is not None:
            cmd += ["-af", filtro_ganho(alvo - self.lufs_arquivo)]
        self.player = subprocess.Popen(cmd + [self.arquivo], creationflags=SEM_JANELA)
        self.tocando_desde, self.tocando_de, self.tocando_ate = time.monotonic(), de, ate
        self.cursor = de
        self.botao_tocar.configure(text="❚❚ Pausar")
        self.animar()

    def tocar_pausar(self):
        if self.tocando():
            self.parar()
        else:
            self.tocar(self.cursor, self.duracao)

    def ouvir(self):
        self.tocar(self.inicio, self.fim)

    def animar(self):
        if not self.player:
            return
        if self.player.poll() is not None:
            self.player = None
            self.botao_tocar.configure(text="▶ Tocar")
            return
        if self.arrastando != "cursor":
            self.cursor = self.posicao_atual()
            self.mover_cursor()
        self.root.after(40, self.animar)

    def parar(self, manter_posicao: bool = True):
        if self.tocando():
            if manter_posicao:
                self.cursor = self.posicao_atual()
            self.player.kill()
        self.player = None
        self.botao_tocar.configure(text="▶ Tocar")
        self.mover_cursor()

    # --- Salvar ---
    def salvar(self):
        if not self.arquivo:
            messagebox.showinfo("Cortar áudio", "Abra um arquivo primeiro.")
            return
        origem = Path(self.arquivo)
        ext = origem.suffix.lower()
        if ext not in (".mp3", ".m4a", ".wav", ".ogg", ".flac"):
            ext = ".mp3"  # vídeo ou formato incomum: salva como MP3
        destino = filedialog.asksaveasfilename(
            initialdir=str(origem.parent), initialfile=f"{origem.stem} (corte){ext}",
            defaultextension=ext, filetypes=TIPOS_SAIDA)
        if not destino:
            return
        if os.path.abspath(destino) == os.path.abspath(self.arquivo):
            messagebox.showerror("Cortar áudio", "Escolha outro nome — não dá para salvar por cima do original.")
            return

        self.botao_salvar.configure(state="disabled")
        self.status.set("Salvando…")
        args = (self.arquivo, self.inicio, self.fim - self.inicio, VOLUMES[self.volume.get()],
                self.fade.get(), destino)
        threading.Thread(target=self.rodar_salvar, args=args, daemon=True).start()

    def rodar_salvar(self, arquivo: str, ini: float, dur: float, alvo: float | None, fade: bool, destino: str):
        filtros, aviso = [], ""
        if alvo is not None:
            self.root.after(0, self.status.set, "Ajustando o volume…")
            calculo = self.calcular_ganho(arquivo, ini, dur, alvo)
            if calculo:
                ganho, original, final = calculo
                filtros.append(filtro_ganho(ganho))
                aviso = f" · volume {original:.1f} → {final:.1f} LUFS ({ganho:+.1f} dB)"
            else:
                aviso = " · trecho em silêncio, volume não alterado"
            self.root.after(0, self.status.set, "Salvando…")
        if fade:
            f = min(0.5, dur / 4)
            filtros += [f"afade=t=in:d={f:.3f}", f"afade=t=out:st={dur - f:.3f}:d={f:.3f}"]

        cmd = [self.ffmpeg, "-y", "-v", "error", "-ss", f"{ini:.3f}", "-i", arquivo,
               "-t", f"{dur:.3f}", "-vn", "-map_metadata", "0"]
        if filtros:
            cmd += ["-af", ",".join(filtros)]
        cmd += CODECS.get(Path(destino).suffix.lower(), []) + [destino]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=SEM_JANELA)
        self.root.after(0, self.salvo, r.returncode == 0, destino, r.stderr.strip(), aviso)

    def salvo(self, ok: bool, destino: str, erro: str, aviso: str = ""):
        self.botao_salvar.configure(state="normal")
        if ok:
            self.status.set(f"Salvo: {destino}{aviso}")
        else:
            self.status.set("Erro ao salvar.")
            messagebox.showerror("Cortar áudio", f"Não consegui salvar:\n\n{erro[-400:]}")
