"""Regressioni per percorsi esterni, persistenza e avvio senza dipendenze LLM."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ConfigurazioneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mmsa test ")
        self.addCleanup(self.tmp.cleanup)
        self.cartella = Path(self.tmp.name)
        self.file_config = self.cartella / "config" / "settings.json"
        self.env = {**os.environ, "MMSA_CONFIG": str(self.file_config), "PYTHONPATH": str(REPO)}

    def salva(self, dati):
        self.file_config.parent.mkdir(parents=True, exist_ok=True)
        self.file_config.write_text(json.dumps(dati), encoding="utf-8")

    def esegui(self, script, *args):
        return subprocess.run([sys.executable, str(REPO / script), *args], cwd=self.cartella,
                              env=self.env, capture_output=True, text=True, timeout=20)

    def python(self, codice):
        r = subprocess.run([sys.executable, "-c", codice], cwd=self.cartella, env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_predefiniti_non_dipendono_dalla_directory_corrente(self):
        r = self.esegui("configurazione.py", "--mostra")
        self.assertEqual(r.returncode, 0, r.stderr)
        dati = json.loads(r.stdout)
        self.assertEqual(dati["RETE"], str(REPO / "base.net.xml"))
        self.assertEqual(dati["LLM_BASE_URL"], "http://127.0.0.1:8080/v1")
        self.assertFalse(self.file_config.exists())

    def test_configurazione_persistente_preserva_parametri_precedenti(self):
        r = self.esegui("configurazione.py", "--llama-dir", "llama esterno", "--modello", "modelli/test.gguf")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.esegui("configurazione.py", "--porta", "9090", "--server-args", '["--jinja", "-c", "4096"]')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.esegui("configurazione.py", "--mostra")
        self.assertEqual(r.returncode, 0, r.stderr)
        dati = json.loads(r.stdout)
        self.assertEqual(dati["LLAMA_CPP_DIR"], str(self.cartella / "llama esterno"))
        self.assertEqual(dati["LLAMA_MODEL"], str(self.cartella / "modelli/test.gguf"))
        self.assertEqual(dati["LLM_BASE_URL"], "http://127.0.0.1:9090/v1")
        self.assertEqual(dati["LLAMA_SERVER_ARGS"], ["--jinja", "-c", "4096"])

    def test_percorsi_relativi_al_file_e_lettura_una_sola_volta(self):
        self.salva({"LLAMA_MODEL": "../modelli/m.gguf", "INDICE": "dati/indice.sqlite", "LLAMA_PORT": 9091})
        self.python("""
import json
from configurazione import CONFIG, FILE_CONFIG, carica_configurazione
assert CONFIG.modello == FILE_CONFIG.parent.parent / 'modelli/m.gguf'
assert CONFIG.indice == FILE_CONFIG.parent / 'dati/indice.sqlite'
FILE_CONFIG.write_text(json.dumps({'LLAMA_PORT': 1234}))
assert carica_configurazione() is CONFIG
assert CONFIG.porta == 9091
""")

    def test_parametri_invalidi_non_sovrascrivono_il_file(self):
        self.salva({"LLAMA_PORT": 8080})
        originale = self.file_config.read_text()
        for args in (("--porta", "0"), ("--modello", ""), ("--server-args", "non json"),
                     ("--server-args", '["--port=1234"]'), ("--server-args", '["--model", "altro.gguf"]')):
            with self.subTest(args=args):
                r = self.esegui("configurazione.py", *args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("Errore:", r.stderr)
                self.assertEqual(self.file_config.read_text(), originale)
        for dati in ({"LLAMA_PORT": True}, {"LLAMA_SERVER_ARGS": "--jinja"}, {"PARAMETRO_ERRATO": 1}, []):
            with self.subTest(dati=dati):
                self.salva(dati)
                self.assertEqual(self.esegui("configurazione.py", "--mostra").returncode, 2)

    def test_url_remoto_e_ipv6(self):
        for dati, url in (({"LLM_BASE_URL": "https://host.example/v1/"}, "https://host.example/v1"),
                          ({"LLAMA_HOST": "::1"}, "http://[::1]:8080/v1"),
                          ({"LLAMA_HOST": "0.0.0.0"}, "http://127.0.0.1:8080/v1")):
            with self.subTest(dati=dati):
                self.salva(dati)
                r = self.esegui("configurazione.py", "--mostra")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout)["LLM_BASE_URL"], url)

    def test_launcher_esegue_binario_esterno_preservando_argomenti(self):
        server = self.cartella / "llama esterno/build/bin/llama-server"
        server.parent.mkdir(parents=True)
        server.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                          "from pathlib import Path\n"
                          "Path(os.environ['MMSA_TEST_ARGV']).write_text(json.dumps(sys.argv[1:]))\n")
        server.chmod(0o755)
        modello = self.cartella / "modelli esterni/test.gguf"
        modello.parent.mkdir()
        modello.touch()
        output = self.cartella / "argv.json"
        self.env["MMSA_TEST_ARGV"] = str(output)
        opzioni = ["--jinja", "--chat-template-kwargs", '{"enable_thinking":false}']
        self.salva({"LLAMA_CPP_DIR": str(server.parents[2]), "LLAMA_MODEL": str(modello),
                    "LLAMA_SERVER_ARGS": opzioni, "LLAMA_PORT": 9092})
        r = self.esegui("avvia_llm.py", "--mostra-comando")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(output.exists())
        r = self.esegui("avvia_llm.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(output.read_text()),
                         ["--model", str(modello), *opzioni, "--host", "127.0.0.1", "--port", "9092"])
        self.salva({"LLAMA_SERVER": str(server), "LLAMA_MODEL": str(modello)})
        self.assertEqual(self.esegui("avvia_llm.py", "--mostra-comando").returncode, 0)

    def test_launcher_segnala_modello_o_binario_mancante(self):
        r = self.esegui("avvia_llm.py", "--mostra-comando")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Modello non configurato", r.stderr)
        modello = self.cartella / "inesistente.gguf"
        self.salva({"LLAMA_MODEL": str(modello), "LLAMA_CPP_DIR": str(self.cartella / "llama")})
        self.assertIn("Modello non trovato", self.esegui("avvia_llm.py").stderr)
        modello.touch()
        r = self.esegui("avvia_llm.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("eseguibile non trovato", r.stderr)

    def test_gui_agente_e_simulazione_condividono_configurazione(self):
        indice = self.cartella / "indice.sqlite"
        with sqlite3.connect(indice) as db:
            db.executescript("""
                CREATE TABLE nodi (id TEXT, lon REAL, lat REAL);
                CREATE TABLE archi (id TEXT, nome TEXT, da_nodo TEXT, a_nodo TEXT, kmh REAL,
                                    corsie INTEGER, lunghezza_m REAL, tipo TEXT, geometria TEXT);
                CREATE TABLE vie (nome TEXT, archi TEXT);
                INSERT INTO vie VALUES ('Via Prova', '[]');
            """)
        self.salva({"INDICE": str(indice), "CARTELLA_SCENARI": "dati/scenari", "CARTELLA_SIM": "dati/sim",
                    "RETE": "rete esterna.net.xml", "VIAGGI": "viaggi esterni.trips.xml", "LLAMA_PORT": 9093})
        self.python("""
from pathlib import Path
import agente, app, simula, test_tool_calling
from configurazione import CONFIG, CARTELLA_PROGETTO
assert agente.CONFIG is simula.CONFIG is test_tool_calling.CONFIG is CONFIG
assert agente.PERCORSO_DB == simula.INDICE == CONFIG.indice
assert simula.RETE == CONFIG.rete and simula.VIAGGI == CONFIG.viaggi
assert simula.CARTELLA_SIM == CONFIG.simulazioni
assert str(agente.client.base_url).rstrip('/') == CONFIG.llm_base_url
assert test_tool_calling.BASE_URL == CONFIG.llm_base_url
assert Path(app.pagina().path) == CARTELLA_PROGETTO / 'static/index.html'
r = agente.crea_scenario({'interventi': [{'tipo': 'velocita', 'via': 'Via Prova', 'kmh': 30}]})
assert r['ok'] and Path(r['file']).parent == CONFIG.scenari
assert Path(r['file']).is_file()
""")


if __name__ == "__main__":
    unittest.main()
