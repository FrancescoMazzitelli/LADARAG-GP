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
'''

# Creiamo un Dumper personalizzato che impedisce la scrittura di alias/ancore
class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True

# Trova tutti i file yaml da processare.
# Uso:
#   python fix_yaml.py                 -> prova /apis/*.yaml, poi ../apis/*.yaml
#   python fix_yaml.py /apis/*.yaml    -> pattern esplicito
#   python fix_yaml.py ./apis          -> directory (auto *.yaml)
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
    
    # 2. Sovrascrive il file scrivendo l'oggetto JSON per esteso (senza alias)
    with open(filepath, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, Dumper=NoAliasDumper, default_flow_style=False, sort_keys=False)
        
    print(f"Risolto: {filepath}")

print(f"Completato! Processati {len(file_trovati)} file YAML senza ancore/alias.")