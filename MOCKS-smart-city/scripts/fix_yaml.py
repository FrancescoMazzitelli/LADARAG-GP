import yaml
import glob
import os
import sys

'''
Script per risolvere il problema degli alias/ancore YAML nei file delle API.
Il problema è che alcuni file YAML usano alias (&) e ancore (*) per evitare la
ripetizione di blocchi di dati, ma questo può causare problemi di compatibilità con alcuni parser YAML.
Questo script legge ogni file YAML, risolve gli alias in memoria e poi riscrive il file senza usare alias,
garantendo che ogni blocco di dati sia scritto per esteso.
Inoltre, pulisce eventuali errori di formattazione nelle regole di Microcks.
'''

# Creiamo un Dumper personalizzato che impedisce la scrittura di alias/ancore
class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True

# Forza le stringhe multilinea a usare il block scalar |
# Senza questo, PyYAML scrive gli script Groovy come stringa inline senza |,
# collassando i newline e rendendo lo script non parsabile da Microcks.
def literal_presenter(dumper, data):
    if '\n' in data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)

NoAliasDumper.add_representer(str, literal_presenter)

# Trova tutti i file yaml da processare.
# Uso:
#   python fix_yaml.py                -> prova /apis/*.yaml, poi ../apis/*.yaml
#   python fix_yaml.py /apis/*.yaml   -> pattern esplicito
#   python fix_yaml.py ./apis         -> directory (auto *.yaml)
if len(sys.argv) > 1:
    input_path = sys.argv[1]
    if os.path.isdir(input_path):
        pattern = os.path.join(input_path, "*.yaml")
    else:
        pattern = input_path
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_container = "/apis/*.yaml"
    candidate_repo = os.path.abspath(os.path.join(script_dir, "..", "apis", "*.yaml"))
    pattern = candidate_container if glob.glob(candidate_container) else candidate_repo

file_trovati = sorted(glob.glob(pattern))

if not file_trovati:
    print(f"Nessun file YAML trovato con pattern: {pattern}")
    sys.exit(1)

for filepath in file_trovati:
    # 1. Legge il file (PyYAML risolve automaticamente * e & in memoria)
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    # PULIZIA: Rimuove spazi e apici "spazzatura" dalle regole di Microcks su tutti gli endpoint.
    # ATTENZIONE: i newline vengono rimossi SOLO per dispatcher non-SCRIPT (es. FALLBACK, URI_PARAMS).
    # Per dispatcher SCRIPT i newline sono parte integrante dello script Groovy e non vanno toccati.
    if data and 'paths' in data:
        for path, methods in data['paths'].items():
            for method, details in methods.items():
                try:
                    if 'x-microcks-operation' in details and 'dispatcherRules' in details['x-microcks-operation']:
                        rules = details['x-microcks-operation']['dispatcherRules']
                        dispatcher = details['x-microcks-operation'].get('dispatcher', '')
                        if isinstance(rules, str):
                            if dispatcher == 'SCRIPT':
                                # Script Groovy multilinea: preserva i newline, solo strip degli spazi esterni.
                                # Il literal_presenter si occuperà di scrivere il blocco con | nel YAML.
                                details['x-microcks-operation']['dispatcherRules'] = rules.strip()
                            else:
                                # JSON/stringa semplice (FALLBACK, URI_PARAMS, RANDOM, ecc.): pulizia normale.
                                clean_rules = rules.strip().replace("\n", "").replace("''", "")
                                details['x-microcks-operation']['dispatcherRules'] = clean_rules
                except Exception:
                    continue  # Ignora eventuali errori strutturali strani e passa al prossimo

    # 2. Sovrascrive il file scrivendo l'oggetto per esteso (senza alias, con block scalar | per multilinea)
    with open(filepath, 'w', encoding='utf-8') as f:
        # width=float("inf") impedisce l'a capo automatico a 80 caratteri
        yaml.dump(data, f, Dumper=NoAliasDumper, default_flow_style=False, sort_keys=False, width=float("inf"))

    print(f"Risolto e pulito: {filepath}")

print(f"Completato! Processati {len(file_trovati)} file YAML senza ancore/alias e con regole Microcks validate.")