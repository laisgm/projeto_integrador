"""PROJETO INTEGRADOR: visualizacao de fluxo termico + ESP32 (somente leitura).

Execute: python projeto_integrador.py
Conecte direto ao ESP32:  python projeto_integrador.py --ip 192.168.1.50
Crie MP4 sem abrir a interface: python projeto_integrador.py --video --saida teste.mp4

O computador precisa estar na MESMA rede Wi-Fi configurada no firmware do ESP32.

A visualizacao e artistica: o DS18B20 mede a temperatura EM UM PONTO.
O estado do ventilador recebido de /status e o comando do rele, nao uma medida de RPM.
"""
from __future__ import annotations

import argparse
import math
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
from PIL import Image, ImageTk

IMG = Path(__file__).resolve().parent / "fluxo_base.png"
FPS = 24
TEMP_LIGA, TEMP_DESLIGA = 28.0, 25.0  # liga SOMENTE >28, desliga SOMENTE <25


class Histerese:
    def __init__(self):
        self.ligado = False

    def atualizar(self, t: float) -> bool:
        if not self.ligado and t > TEMP_LIGA:
            self.ligado = True
        elif self.ligado and t < TEMP_DESLIGA:
            self.ligado = False
        return self.ligado


class RenderizadorVento:
    """Anima a textura de ar (deformacao + brilho correndo pelos filamentos)."""

    def __init__(self, w: int = 800, h: int = 450):
        img = cv2.imread(str(IMG), cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"Imagem nao encontrada: {IMG}")
        self.azul = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)
        i = self.azul.max(axis=2)
        self.vermelho = np.clip(np.stack([i * .05, i * .22, i * 1.06], axis=2), 0, 255)  # BGR laranja
        # Malhas precomputadas: a cena fica parada, os fios luminosos ondulam.
        self.x = x = np.arange(w, dtype=np.float32)[None, :]
        self.y = y = np.arange(h, dtype=np.float32)[:, None]
        self.fases = (y * .028 + x * .011, y * .016 - x * .018, x * .009 + y * .006)
        self.brilhos = (x * .045 + y * .018, x * .021 - y * .037)
        self._chave = self._base = None

    def quadro(self, t: float, temp: float | None, cooler: bool | None,
               modo: str = "SIMULACAO") -> np.ndarray:
        ok = temp is not None and math.isfinite(temp)
        mistura = float(np.clip(((temp if ok else 22.0) - 23.0) / 7.5, 0, 1))
        chave = round(mistura, 3)
        if chave != self._chave:
            self._base = cv2.addWeighted(self.azul, 1 - chave, self.vermelho, chave, 0.0)
            self._chave = chave

        f1, f2, f3 = self.fases
        o1, o2, o3 = np.sin(f1 - t * 2.3), np.sin(f2 + t * 1.85), np.sin(f3 + t * 1.2)
        dx = (5.0 * o1 + 2.4 * o2 + 1.8 * o3).astype(np.float32)
        dy = (2.1 * o1 - 2.2 * o2 + 2.1 * o3).astype(np.float32)
        mov = cv2.remap(self._base, self.x + dx, self.y + dy, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT)
        b1, b2 = self.brilhos  # faixas de luz viajando pelos rastros
        mov *= (.94 + .11 * np.sin(b1 + t * 7.2) + .055 * np.sin(b2 - t * 6.5)).astype(np.float32)[:, :, None]
        frame = np.clip(mov, 0, 255).astype(np.uint8)

        # HUD pequeno: o fluxo continua sendo a parte principal da imagem.
        cv2.rectangle(frame, (15, 15), (345, 118), (7, 11, 21), -1)
        cv2.rectangle(frame, (15, 15), (19, 118), (30, 160, 252) if mistura > .6 else (247, 190, 40), -1)
        estado = "SEM DADOS" if cooler is None else "LIGADO" if cooler else "DESLIGADO"
        for texto, pos, esc, cor, esp in (
            ("PROJETO INTEGRADOR | " + modo, (29, 37), .39, (182, 192, 210), 1),
            (f"{temp:04.1f} C" if ok else "--.- C", (27, 78), 1.18, (252, 248, 248), 2),
            ("COOLER: " + estado, (29, 100), .46, (104, 224, 122) if cooler else (180, 181, 187), 1),
        ):
            cv2.putText(frame, texto, pos, cv2.FONT_HERSHEY_SIMPLEX, esc, cor, esp, cv2.LINE_AA)
        return frame


def simular_video(destino: Path, duracao: float = 12, fps: int = FPS, informar_progresso=None):
    """Exporta um MP4 com o ciclo 22-34 C, sem precisar do sensor real."""
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("Instale: pip install imageio-ffmpeg") from exc

    w, h = 960, 540
    render, hist = RenderizadorVento(w, h), Histerese()
    n = max(1, round(duracao * fps))
    destino.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(destino)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    tempo_ar = 0.0
    try:
        for i in range(n):
            u = i / max(1, n - 1)
            temp = 22.0 + 12.0 * u * u * (3 - 2 * u)
            ligado = hist.atualizar(temp)
            tempo_ar += (1.8 if ligado else .35) / fps
            proc.stdin.write(render.quadro(tempo_ar, temp, ligado, "DEMONSTRACAO").tobytes())
            if informar_progresso and i % 12 == 0:
                informar_progresso(round(100 * i / n))
    finally:
        proc.stdin.close()
    erro = proc.stderr.read().decode("utf-8", "replace")
    if proc.wait():
        raise RuntimeError(f"Erro ao gerar video MP4: {erro[-500:]}")
    if informar_progresso:
        informar_progresso(100)
    return destino


FUNDO, PAINEL = "#0a0f19", "#111b2b"
BOTAO = dict(bg="#173148", fg="#ebf5ff", activebackground="#225578", activeforeground="white",
             font=("Segoe UI", 9, "bold"), pady=7, bd=0, cursor="hand2")


class Janela:
    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox, simpledialog
        self.dialogo, self.aviso = simpledialog, messagebox
        self.janela = j = tk.Tk()
        j.title("PROJETO INTEGRADOR | Controle termico")
        j.configure(bg=FUNDO)
        j.resizable(False, False)

        self.render, self.histerese, self.mensagens = RenderizadorVento(), Histerese(), queue.Queue()
        self.auto, self.modo, self.esp32_url, self.conexao_id = False, "demo", "", 0
        self.temperatura = 22.0
        self.cooler = self.histerese.atualizar(self.temperatura)
        self.rodando, self.tempo_ar, self.ultimo_hud = True, 0.0, -10.0
        self.ultimo_quadro, self.imagem_tk = time.monotonic(), None
        self.comunicacao = threading.Event()
        self.comunicacao.set()

        def faixa(bg=FUNDO, **pack):
            f = tk.Frame(j, bg=bg)
            f.pack(fill="x", **pack)
            return f

        def rotulo(pai, texto, tam, cor, negrito=False, bg=FUNDO):
            fonte = ("Segoe UI", tam, "bold") if negrito else ("Segoe UI", tam)
            return tk.Label(pai, text=texto, font=fonte, fg=cor, bg=bg)

        cab = faixa(padx=20, pady=(14, 7))
        rotulo(cab, "PROJETO INTEGRADOR", 20, "#e9f1ff", True).pack(side="left")
        rotulo(cab, "  DIGITAL AIRFLOW LAB", 9, "#45bff8").pack(side="left", pady=(11, 0))
        self.rotulo_origem = rotulo(cab, "● DEMONSTRAÇÃO", 10, "#f4c45b", True)
        self.rotulo_origem.pack(side="right", pady=(9, 0))

        self.tela = tk.Label(j, bg="black", bd=1, relief="solid")
        self.tela.pack(padx=18, pady=(0, 6))

        info = faixa(PAINEL, padx=18, pady=(0, 9), ipady=8)
        self.temp_texto = rotulo(info, "22.0 °C", 15, "#75cafa", True, PAINEL)
        self.temp_texto.pack(side="left", padx=(18, 22))
        self.cooler_texto = rotulo(info, "COOLER DESLIGADO", 11, "#a7b3c4", True, PAINEL)
        self.cooler_texto.pack(side="left")
        rotulo(info, "Liga >28 °C  |  Desliga <25 °C", 9, "#95a5b9", bg=PAINEL).pack(side="right", padx=16)

        cmd = faixa(padx=18)
        rotulo(cmd, "TEMPERATURA DE TESTE", 9, "#c0cada", True).pack(anchor="w")
        self.slider = tk.Scale(cmd, from_=15, to=45, resolution=.1, orient="horizontal", bg=FUNDO,
                               fg="#cce5fb", troughcolor="#213e5e", activebackground="#37c4fa",
                               highlightthickness=0, length=770, command=self.mudar_slider)
        self.slider.pack(side="left", padx=(0, 12))
        self.slider.set(self.temperatura)
        self.botao_ciclo = tk.Button(cmd, text="INICIAR CICLO", command=self.alternar_ciclo, padx=10, **BOTAO)
        self.botao_ciclo.pack(side="left")

        con = faixa(padx=18, pady=(7, 3))
        tk.Button(con, text="CONECTAR ESP32", command=self.conectar, padx=12, **BOTAO).pack(side="left")
        tk.Button(con, text="MODO DEMO", command=self.modo_demo, padx=12, **BOTAO).pack(side="left", padx=(9, 0))

        self.status = rotulo(faixa(padx=18, pady=(9, 12)),
                             "Modo demonstração: arraste a barra ou inicie o ciclo automático.", 9, "#8fa1b6")
        self.status.pack(side="left")

        threading.Thread(target=self.ler_esp32, daemon=True).start()
        j.protocol("WM_DELETE_WINDOW", self.fechar)
        self.animar()

    # ---------- modos ----------
    def mudar_slider(self, valor):
        if not self.auto and self.modo == "demo":
            self.temperatura = float(valor)
            self.cooler = self.histerese.atualizar(self.temperatura)

    def alternar_ciclo(self):
        if self.modo != "demo":
            self.modo_demo()
        self.auto = not self.auto
        self.botao_ciclo.config(text="PARAR CICLO" if self.auto else "INICIAR CICLO")
        self.slider.config(state="disabled" if self.auto else "normal")
        self.status.config(text="Simulando aquecimento e resfriamento..." if self.auto
                           else "Modo manual: arraste a barra para testar o controle.")

    def _trocar_modo(self, modo, origem, status):
        self.conexao_id += 1
        self.modo, self.auto = modo, False
        self.botao_ciclo.config(text="INICIAR CICLO")
        self.slider.config(state="normal" if modo == "demo" else "disabled")
        self.rotulo_origem.config(text=origem, fg="#f4c45b")
        self.status.config(text=status)

    def modo_demo(self):
        self._trocar_modo("demo", "● DEMONSTRAÇÃO", "Simulação local. Não aciona o relé físico.")
        self.temperatura = float(self.slider.get())
        self.histerese.ligado = False
        self.cooler = self.histerese.atualizar(self.temperatura)

    def conectar(self):
        # O IP aparece somente nesta caixa de diálogo, não no painel.
        end = self.dialogo.askstring("Conectar ESP32", "Informe o IP local do ESP32 (ex.: 192.168.1.50):",
                                     parent=self.janela, initialvalue=self.esp32_url.removeprefix("http://"))
        if end is not None:
            self.conectar_endereco(end)

    def conectar_endereco(self, end: str):
        if not (end := end.strip()):
            return self.aviso.showwarning("ESP32", "Informe o endereço do ESP32.")
        p = urlparse(end if "://" in end else "http://" + end)
        if p.scheme != "http" or not p.hostname or p.username or p.password:
            return self.aviso.showerror("ESP32", "Informe um IP ou endereço HTTP válido.")
        try:
            porta = p.port
        except ValueError:
            return self.aviso.showerror("ESP32", "Porta HTTP inválida.")
        if p.path not in ("", "/") or p.query or p.fragment:
            return self.aviso.showerror("ESP32", "Digite somente o endereço do ESP32, sem caminhos.")
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        self.esp32_url = f"http://{host}" + (f":{porta}" if porta else "")
        self._trocar_modo("real", "● CONECTANDO ESP32", "Conectando ao ESP32 pela rede Wi-Fi...")
        self.temperatura = self.cooler = None

    # ---------- comunicação ----------
    def ler_esp32(self):
        with requests.Session() as sessao:
            while self.comunicacao.is_set():
                if self.modo != "real" or not self.esp32_url:
                    time.sleep(.15)
                    continue
                url, tentativa = self.esp32_url, self.conexao_id
                try:
                    rt = sessao.get(url + "/temperaturec", timeout=1.5)
                    rs = sessao.get(url + "/status", timeout=1.5)
                    rt.raise_for_status()
                    rs.raise_for_status()
                    temp = float(rt.text.strip().replace(",", "."))
                    if not (math.isfinite(temp) and -55 <= temp <= 125):
                        raise ValueError("Leitura de temperatura inválida")
                    estado = rs.text.strip().upper()
                    if estado not in ("LIGADO", "DESLIGADO"):
                        raise ValueError("Estado do cooler inválido")
                    self.mensagens.put((tentativa, "dado", temp, estado == "LIGADO"))
                except (requests.RequestException, ValueError) as erro:
                    self.mensagens.put((tentativa, "erro", str(erro)))
                # Lê a cada 2 s, mas permite alternar para o modo demo.
                for _ in range(20):
                    if not self.comunicacao.is_set() or self.modo != "real" or tentativa != self.conexao_id:
                        break
                    time.sleep(.1)

    def atualizar_mensagens(self):
        while True:
            try:
                msg = self.mensagens.get_nowait()
            except queue.Empty:
                return
            if msg[0] != self.conexao_id or self.modo != "real":
                continue
            ok = msg[1] == "dado"
            self.temperatura, self.cooler = (msg[2], msg[3]) if ok else (None, None)
            self.rotulo_origem.config(text="● ESP32 ONLINE" if ok else "● ESP32 OFFLINE",
                                      fg="#55d995" if ok else "#ff6673")
            self.status.config(text="Lendo temperatura e estado do relé pelo ESP32." if ok
                               else "Sem conexão. Confira o IP e o Wi-Fi e tente novamente.")

    # ---------- animação ----------
    def animar(self):
        if not self.rodando:
            return
        agora = time.monotonic()
        dt = min(.12, agora - self.ultimo_quadro)
        self.ultimo_quadro = agora

        self.atualizar_mensagens()
        if self.auto and self.modo == "demo":
            self.temperatura = max(20, min(37, self.temperatura + (-1.55 if self.cooler else 1.20) * dt))
            self.cooler = self.histerese.atualizar(self.temperatura)
            self.slider.set(round(self.temperatura, 1))

        self.tempo_ar += dt * (1.9 if self.cooler else .35)
        quadro = self.render.quadro(self.tempo_ar, self.temperatura, self.cooler,
                                    "ESP32 REAL" if self.modo == "real" else "SIMULACAO")
        self.imagem_tk = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(quadro, cv2.COLOR_BGR2RGB)))
        self.tela.config(image=self.imagem_tk)

        if agora - self.ultimo_hud > .25:
            self.ultimo_hud = agora
            t = self.temperatura
            self.temp_texto.config(text=f"{t:.1f} °C" if t is not None else "--.- °C",
                                   fg="#ff6673" if t is not None and t > 28 else "#65caff")
            estado = "SEM DADOS" if self.cooler is None else "LIGADO" if self.cooler else "DESLIGADO"
            self.cooler_texto.config(text="COOLER " + estado, fg="#55d995" if self.cooler else "#a8b6c7")
        self.janela.after(40, self.animar)

    def fechar(self):
        self.rodando = False
        self.comunicacao.clear()
        self.janela.destroy()

    def iniciar(self):
        self.janela.mainloop()


def main():
    parser = argparse.ArgumentParser(description="PROJETO INTEGRADOR - fluxo de ar animado")
    parser.add_argument("--video", action="store_true", help="Exportar MP4 sem abrir a janela")
    parser.add_argument("--saida", default="projeto_integrador_demo.mp4")
    parser.add_argument("--duracao", type=float, default=12.0)
    parser.add_argument("--ip", help="Conecta direto ao ESP32 (ex.: 192.168.1.50 ou 127.0.0.1:8080)")
    args = parser.parse_args()
    if args.video:
        print(f"Video criado: {simular_video(Path(args.saida), args.duracao).resolve()}")
    else:
        janela = Janela()
        if args.ip:
            janela.conectar_endereco(args.ip)
        janela.iniciar()


if __name__ == "__main__":
    main()
