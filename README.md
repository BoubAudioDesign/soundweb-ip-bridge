# Passerelle IP pour BSS Soundweb 9088 (SW9088 / 9088ii / 9088iis)

Pilote un Soundweb 9088 depuis le réseau IP (télécommande, Crestron, AMX, Extron, Companion/Stream Deck, navigateur…)
via son **port RS-232 arrière**. Fonction principale : **changement de source à distance**, avec retour d'état.

Implémente le protocole officiel BSS du *Soundweb Serial Interface Kit* (SDK2.pdf, disponible sur bssaudio.com, page
Soundweb Designer → « Serial Interface SDK ») : 38400 bauds 8N1, trames `STX … checksum ETX`, échappement `0x1B`,
ACK/NAK sous 1 s, renvoi automatique.

```
Télécommande IP ──TCP 4999 / HTTP 8080──► passerelle (Pi / mini-PC Linux ou Windows) ──RS-232 38400──► Soundweb 9088 (port arrière)
```

## 1. Préparer le Soundweb (Soundweb Designer)

1. Dans le design, l'objet **Source Selector** de chaque zone existe déjà (ou ajoute-le).
2. **Renomme l'objet device** pour que son nom se termine par `+s` (ex. `Salle +s`). C'est ce qui bascule le port
   RS-232 arrière en mode « messages Soundweb » au lieu du mode connexion Soundweb Designer. **Redémarre le 9088**.
3. Compile et charge le design.
4. Survole chaque Source Selector : le **handle** s'affiche dans la barre d'état en bas à gauche. Note-le.
5. (Optionnel, pour le retour d'état) place les Source Selectors sur la fenêtre **Serial Input/Output** du device :
   un changement fait ailleurs (PC, contacts de contrôle) sera alors renvoyé à la passerelle.
6. (Optionnel) note l'index des **presets** (0, 1, 2… en partant du haut de la liste des presets).

### 1 bis. Alternative : garder le port arrière en mode Soundweb Designer (`protocol: designer`)

Si Soundweb Designer doit rester connectable sur le port arrière (pas de câble pour le port avant), **ne pas** ajouter
`+s` et mettre dans `config.yaml` :

```yaml
serial:
  port: COM4               # ou /dev/ttyUSB0
  protocol: designer       # 115200 bauds + RTS/CTS par défaut
```

La passerelle parle alors le protocole de Soundweb Designer (non documenté par BSS, reconstitué à partir de captures) :
elle ouvre une session comme Designer (identification, réception de la config, passage en ligne), la maintient
(keep-alive) et se reconnecte automatiquement.

- Commandes disponibles dans ce mode : **rappel de preset** (`PRESET`, et `SOURCE` pour les sources configurées avec
  `preset:`, voir § 4) et **volumes** (`VOLUME`, slider de la page web), ainsi que `RAW` pour un paramètre 16 bits
  (méthode `0x01xxxxxx`). Les sources pilotées par `handle`, `HEX`, `SETVALUE` et les parameter presets répondent `ERR`.
- La valeur des volumes est lue dans la config du 9088 à la connexion, puis suivie en temps réel (le 9088 signale
  les changements, par exemple après un rappel de preset).
- **Preset actif au démarrage** : le 9088 ne mémorise pas de numéro de preset actif, mais chaque preset laisse une
  « signature » (les valeurs des paramètres qu'il règle). Apprendre ces signatures une fois (le son peut changer,
  le preset actif est restauré à la fin) ; à refaire après chaque modification du design :

  ```powershell
  .\.venv\Scripts\python.exe -m soundweb_bridge -c config.yaml --learn-presets
  ```

  Elles sont enregistrées dans `preset-signatures.json` (à côté de `config.yaml`). Au démarrage, la passerelle
  reconnaît le preset actif et l'affiche sur la page ; `PRESET?` (TCP) le renvoie.
- Le port COM ne peut être ouvert que par un seul logiciel : **arrêter la passerelle avant de connecter Designer**
  (Ctrl+C ferme proprement la session).
- `--simulate` fonctionne aussi dans ce mode (simulateur du protocole Designer).

## 2. Câblage

Port RS-232 **arrière** du 9088 ↔ adaptateur USB-série (FTDI recommandé) avec un câble **null-modem 3 fils**
(TX↔RX croisés + masse). Le Soundweb fonctionne en 38400 8N1 sans contrôle de flux dans ce mode.
Un convertisseur série/IP (Moxa, Lantronix…) fonctionne aussi : `port: socket://IP:PORT`.

## 3a. Installation Linux (Raspberry Pi OS / Debian / Ubuntu)

```bash
sudo useradd -r -G dialout soundweb
sudo git clone <ce dépôt> /opt/soundweb-ip-bridge && cd /opt/soundweb-ip-bridge
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo mkdir -p /etc/soundweb-bridge && sudo cp config.example.yaml /etc/soundweb-bridge/config.yaml
sudo nano /etc/soundweb-bridge/config.yaml          # port série, handles, noms des sources
sudo cp deploy/soundweb-bridge.service /etc/systemd/system/
sudo systemctl enable --now soundweb-bridge
journalctl -u soundweb-bridge -f                    # logs (ajouter -v à ExecStart pour voir les trames hex)
```

Essai sans matériel : `python3 -m soundweb_bridge -c config.example.yaml --simulate` puis http://localhost:8080.

## 3b. Installation Windows (10 / 11, Python 3.8+ installé)

Dans PowerShell, depuis le dossier du projet :

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1     # .venv + dépendances + config.yaml + liste des ports COM
notepad config.yaml                                                     # serial.port: COM3 (cf. liste affichée), handles, sources
.\.venv\Scripts\python.exe -m soundweb_bridge -c config.yaml --simulate  # essai sans matériel -> http://localhost:8080
deploy\windows\start.bat                                                # lancement réel au premier plan (ajouter -v : trames hex)
```

- Port série : `.\.venv\Scripts\python.exe -m soundweb_bridge --list-ports` (ou Gestionnaire de périphériques →
  *Ports (COM et LPT)*). Le port COM ne doit pas être ouvert par un autre logiciel (Soundweb Designer, terminal série…).
- **Démarrage automatique** (équivalent du service systemd), dans un PowerShell **administrateur** :

  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1    # options : -Config C:\...\config.yaml  -Hex
  Get-Content -Wait logs\bridge.log                                             # suivre les logs
  Stop-ScheduledTask SoundwebBridge; Start-ScheduledTask SoundwebBridge          # redémarrer après modif. de config.yaml
  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-service.ps1  # désinstaller
  ```

  Crée une tâche planifiée `SoundwebBridge` (compte SYSTEM, lancée au démarrage de Windows sans session ouverte,
  relancée automatiquement) et ouvre les ports HTTP/TCP dans le pare-feu Windows.
- Test sans `nc` : `Invoke-WebRequest http://localhost:8080/api/selectors/salle/source/2`, ou PuTTY en mode *Raw*
  sur le port 4999.

## Outils de diagnostic

| Commande | Rôle |
|---|---|
| `python -m soundweb_bridge --list-ports` | liste les ports série (COMx, /dev/tty…) |
| `python -m soundweb_bridge -c config.yaml --probe [PORT]` | mode `+s` : teste vitesses / contrôle de flux avec une trame volontairement invalide (le 9088 répond NAK, rien n'est exécuté) |
| `python -m soundweb_bridge.sniff --pc COM11 --device COM4 --baud 115200 --rtscts` | capture le trafic Designer ↔ 9088 via une paire de ports virtuels (com0com ; Designer n'accepte que COM1 à COM4) ; taper une note + Entrée pour annoter |

## Simulateur autonome

Pour tester un automate / une télécommande sans Soundweb : `python -m soundweb_bridge.simulator`.
Linux : pseudo-terminal (chemin affiché, à mettre dans `serial.port`). Windows : port TCP 5000 →
`serial.port: socket://127.0.0.1:5000`. `--tcp <port>` force le mode TCP (joignable depuis une autre machine).
Taper `<handle> <source>` simule un changement fait côté Soundweb (retour d'état).
`--designer` simule le port arrière en mode Designer (TCP, port 5000 par défaut) pour `protocol: designer`.

## 4. Configuration

Voir `config.example.yaml`. Pour chaque zone :

```yaml
selectors:
  salle:                 # nom utilisé dans les commandes (sans espace)
    label: Salle principale
    handle: 0x00000005   # handle du Source Selector (RAW_MSG, méthode recommandée)
    sources: {1: PC pupitre, 2: Lecteur Blu-ray, 3: Micro HF}
```

Alternative si tu utilises le mode « RS232 Control window » / codes de canal (comme les modules Crestron historiques) :
remplace `handle` par `set_value_id: <code canal>` → envoi d'un `SET_VALUE` groupe Source (6).

### Sélection de source par rappel de preset

Une source peut déclencher un **preset** au lieu d'agir sur le Source Selector : utile quand un choix de source doit
aussi changer des niveaux, des routages ou plusieurs zones à la fois.

```yaml
presets:
  Conference: 0
selectors:
  auditorium:
    label: Auditorium
    sources:
      1: {label: Conférence, preset: Conference}          # preset global (nom de la section presets, ou index)
      2: {label: Concert, preset: 3}
      3: {label: Visio, parameter_preset: [2, 1]}         # parameter preset : [id, index d'état]
  hall:
    handle: 0x00000009                                    # mixte : sources sans preset -> Source Selector
    sources: {1: Ambiance, 2: {label: Concert, preset: 3}}
```

- Les commandes ne changent pas : `SOURCE auditorium 2`, `/api/selectors/auditorium/source/2`, boutons de la page web.
- `handle` / `set_value_id` ne sont obligatoires que si au moins une source n'a pas de preset.
- Mise à jour de l'état : quand un preset est rappelé (par `SOURCE`, `PRESET`, `PPRESET`, HTTP, ou renvoyé par le
  Soundweb), **toutes** les zones dont une source est liée à ce preset passent sur cette source.
- Index de preset : position dans la liste des presets de Soundweb Designer, en partant de 0 (cf. § 1, étape 6).

## 5. Piloter

### Page web (télécommande)
`http://<passerelle>:8080/` (ajoute `?token=…` si un token est configuré). Un slider par volume et un bouton par
preset (le dernier rappelé est mis en évidence), état en temps réel sur toutes les télécommandes ouvertes.

### Volumes

```yaml
volumes:
  master:
    label: Volume
    handle: 5            # handle de l'objet potentiomètre / gain (barre d'état de Soundweb Designer)
    parameter: 0x30      # paramètre de gain (forme courte de la méthode 0x01000830)
    min_db: -100         # bas du curseur, affiché « −∞ »
    max_db: 0
    # scale: 256         # valeur transmise = dB × 256 (entier signé 16 bits)
    # law: log           # loi du curseur : log (fader : 50 % = −12 dB, 10 % = −40 dB) ou linear
    # keep_on_preset: true  # au rappel d'un preset, le 9088 réapplique la position du potentiomètre physique :
    #                       # la passerelle garde (et renvoie) le dernier volume connu. false = suivre le 9088
```

### TCP texte — port 4999 (Crestron TCP/IP Client, AMX, Extron, Companion « Generic TCP », netcat…)
Une commande par ligne, terminée par CR/LF. Insensible à la casse pour les labels.

| Commande | Réponse |
|---|---|
| `SOURCE salle 2` / `SOURCE salle Lecteur Blu-ray` | `OK SOURCE salle 2` |
| `SOURCE salle 0` (aucune source) | `OK SOURCE salle 0` |
| `SOURCE? salle` (ou `SOURCE?` pour tout) | `SOURCE salle 2` (`?` si inconnue) |
| `PRESET Show` / `PRESET 3` | `OK PRESET 0` |
| `PRESET?` | `PRESET 0` (`?` si inconnu) |
| `VOLUME master -20` / `VOLUME master min` / `VOLUME master max` | `OK VOLUME master -20` |
| `VOLUME? master` (ou `VOLUME?` pour tout) | `VOLUME master -8.42` (`?` si inconnu) |
| `PPRESET <id preset paramètre> <état>` | `OK PPRESET` |
| `RAW <handle> <méthode> <valeur>` | `OK RAW` (accès à n'importe quel objet, cf. annexe A du SDK) |
| `SETVALUE <groupe> <id> <valeur>` | `OK SETVALUE` |
| `HEX 84 00 00 00 05 01 00 08 00 00 02` | `OK HEX` (corps brut, tramé automatiquement) |
| `STATUS`, `PING`, `HELP` | |

Erreurs : `ERR <raison>` (ex. `ERR message not acknowledged after 3 attempts: no ACK (timeout)`).
Messages spontanés envoyés à tous les clients : `SOURCE <zone> <n>`, `PRESET <id>`, `LINK UP` / `LINK DOWN`.

Test rapide (Linux) : `printf 'SOURCE salle 2\r\n' | nc -q1 <passerelle> 4999`

### HTTP — port 8080
| Méthode | URL | |
|---|---|---|
| GET ou POST | `/api/selectors/salle/source/2` | change la source (GET pratique pour les télécommandes simples) |
| POST | `/api/selectors/salle/source` + `{"source": "Micro HF"}` | |
| GET | `/api/selectors/salle/source` | source courante |
| GET ou POST | `/api/presets/Show` | rappel de preset |
| GET ou POST | `/api/volumes/master/-20` | règle un volume (dB, `min`, `max`, `-inf`) |
| POST | `/api/volumes/master` + `{"db": -20}` | |
| GET | `/api/volumes/master` | volume courant |
| POST | `/api/raw` + `{"handle":5,"method":"0x01000800","value":2}` | message brut |
| GET | `/api/state` | état complet (JSON) |
| WS | `/ws` | événements temps réel (JSON) ; accepte `{"action":"preset","preset":"Show"}`, `{"action":"volume","volume":"master","db":-20}` |

Token : `?token=…` ou en-tête `Authorization: Bearer …`.

## Notes / limites

- Le Soundweb **ne renvoie pas** les changements qu'il reçoit : la passerelle met à jour l'état dès l'ACK.
  Au démarrage l'état est « inconnu » tant qu'aucun changement n'a eu lieu (le protocole d'origine ne permet pas de lire
  une valeur : `REQUEST_VALUE` est « réservé »).
- Ordre des octets : big-endian (conforme à l'exemple du SDK pour `SET_VALUE`). Pour valider sur site, l'outil
  `SWMsgTool.exe` fourni dans le SDK affiche les octets exacts.
- Pas de chiffrement : à utiliser sur un réseau de contrôle AV isolé, ou activer le token.

## Tests

```bash
pip install -r requirements.txt pytest && python3 -m pytest -q          # Linux
.\.venv\Scripts\python.exe -m pip install pytest                       # Windows
.\.venv\Scripts\python.exe -m pytest -q
```
Les tests utilisent un Soundweb simulé (pty sous Linux, TCP local sous Windows) : ACK/NAK, renvoi, retour d'état,
TCP et HTTP.
