"""
Arène — gestion de compétitions de jeux vidéo avec Streamlit.

- L'admin propose un jeu (joueurs par match, qualifiés par match, date, heure, lieu).
- Les participants créent un compte (pseudo, e-mail, mot de passe), se connectent,
  puis cliquent sur « Participer » : un e-mail = un compte = une seule participation
  par compétition.
- L'admin lance la compétition : l'app génère les tours d'élimination.
  * Jeu en duel (2 joueurs par match) : 1 gagnant qualifié par match.
  * Jeu à 3 joueurs ou plus : les joueurs sont répartis en groupes de la
    taille du jeu, les meilleurs de chaque match se qualifient.
- Le dernier tour (finale) réunit tous les joueurs restants dans un seul match.
- L'accès admin exige l'adresse e-mail d'un admin + le mot de passe admin.

Lancement : streamlit run app.py

Configuration (variables d'environnement ou st.secrets) :
  ADMIN_PASSWORD : mot de passe admin (par défaut : "gaming for all")
  ADMIN_EMAILS   : adresses admin séparées par des virgules
                   (par défaut : ninon.ombeandonga@2030.ucac-icam.com,
                   jac.de@2030.ucac-icam.com)

Notifications par e-mail (confirmation d'inscription, changement de planning,
convocations à chaque tour, annonces de l'admin). Sans SMTP_HOST, l'app
fonctionne mais n'envoie aucun e-mail :
  SMTP_HOST      : serveur SMTP (ex : smtp.gmail.com)
  SMTP_PORT      : 587 (STARTTLS, par défaut) ou 465 (SSL)
  SMTP_USER      : identifiant du compte d'envoi
  SMTP_PASSWORD  : mot de passe (pour Gmail : un « mot de passe d'application »)
  SMTP_FROM      : adresse d'expéditeur (par défaut : SMTP_USER)

Sécurité (actives seulement si SMTP est configuré) :
  REQUIRE_EMAIL_VERIFICATION : "true" (défaut) = code à 6 chiffres envoyé par e-mail
                               pour activer un nouveau compte ; "false" pour désactiver
  ADMIN_EMAIL_CODE           : "true" (défaut) = code envoyé à l'e-mail de l'admin à chaque
                               connexion admin ; "false" pour désactiver
Le mot de passe oublié par e-mail fonctionne aussi uniquement avec SMTP.
"""

import csv
import datetime
import hashlib
import hmac
import io
import math
import os
import random
import re
import secrets
import smtplib
import sqlite3
import ssl
from contextlib import closing
from email.message import EmailMessage

import streamlit as st

DB_PATH = os.environ.get("ARENA_DB", "arena.db")
DEFAULT_ADMIN_PASSWORD = "gaming for all"
DEFAULT_ADMIN_EMAILS = "ninon.ombeandonga@2030.ucac-icam.com,jac.de@2030.ucac-icam.com"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_SIGNUP_METHOD = (
    "1. Crée ton compte dans l'onglet « Mon compte » (pseudo, e-mail, mot de passe).\n"
    "2. Connecte-toi.\n"
    "3. Clique sur « Participer » sur la page de la compétition."
)
MIN_PASSWORD_LENGTH = 8
CODE_TTL_MINUTES = 15          # durée de validité d'un code e-mail
CODE_MAX_ATTEMPTS = 5          # essais maximum par code
CODE_COOLDOWN_SECONDS = 60     # délai minimum entre deux envois de code
LOGIN_MAX_FAILURES = 5         # échecs de connexion avant blocage
LOGIN_LOCK_MINUTES = 10        # durée du blocage
UNVERIFIED_TTL_HOURS = 24      # un compte non vérifié est supprimé après ce délai

st.set_page_config(page_title="Arène", page_icon="🎮", layout="wide")


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
def get_setting(name, default):
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name, default)


def admin_emails():
    raw = get_setting("ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS)
    return {e.strip().lower() for e in str(raw).split(",") if e.strip()}


def valid_email(email):
    return bool(EMAIL_RE.match(email))


def admin_password():
    return str(get_setting("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD))


# Fragments : seule la zone concernée est rafraîchie (et non toute la page) quand on
# interagit avec elle. Nécessite Streamlit >= 1.37 ; sinon, comportement habituel.
fragment = (getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
            or (lambda func: func))


def rerun_fragment():
    """Relance uniquement le fragment en cours, sans recharger toute la page."""
    try:
        st.rerun(scope="fragment")
    except TypeError:  # Streamlit trop ancien
        st.rerun()
    except Exception as exc:  # StreamlitAPIException si on n'est pas dans un fragment
        if exc.__class__.__name__ != "StreamlitAPIException":
            raise
        st.rerun()


# ----------------------------------------------------------------------------
# Base de données
# ----------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # lectures/écritures simultanées
    return conn


def query(sql, params=()):
    with closing(get_conn()) as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql, params=()):
    with closing(get_conn()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def ensure_column(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with closing(get_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                match_size INTEGER NOT NULL DEFAULT 2,
                qualifiers INTEGER NOT NULL DEFAULT 1,
                max_players INTEGER,
                status TEXT NOT NULL DEFAULT 'inscriptions',
                current_round INTEGER NOT NULL DEFAULT 0,
                event_date TEXT,
                event_time TEXT,
                location TEXT DEFAULT '',
                signup_method TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                pseudo TEXT NOT NULL,
                email TEXT,
                UNIQUE(game_id, pseudo)
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                round INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                is_final INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS match_players (
                match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
                place INTEGER,
                PRIMARY KEY (match_id, participant_id)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER REFERENCES games(id) ON DELETE CASCADE,
                kind TEXT,
                subject TEXT,
                total INTEGER,
                sent INTEGER,
                failed INTEGER,
                error TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pseudo TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS verification_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            """
        )
        # Migration des bases créées avec une version précédente
        ensure_column(conn, "games", "event_date", "TEXT")
        ensure_column(conn, "games", "event_time", "TEXT")
        ensure_column(conn, "games", "location", "TEXT DEFAULT ''")
        ensure_column(conn, "games", "signup_method", "TEXT DEFAULT ''")
        ensure_column(conn, "participants", "email", "TEXT")
        ensure_column(conn, "participants", "user_id", "INTEGER")
        # Les comptes créés avant la vérification par e-mail restent valides (verified = 1)
        ensure_column(conn, "users", "verified", "INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_participant_email "
            "ON participants(game_id, email)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_participant_user "
            "ON participants(game_id, user_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_pseudo "
            "ON users(pseudo COLLATE NOCASE)"
        )
        conn.commit()


# ----------------------------------------------------------------------------
# Planning (date, heure, lieu)
# ----------------------------------------------------------------------------
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def format_schedule(game):
    parts = []
    if game["event_date"]:
        d = datetime.date.fromisoformat(game["event_date"])
        txt = f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"
        if game["event_time"]:
            txt += f" à {game['event_time']}"
        parts.append("📅 " + txt)
    else:
        parts.append("📅 Date à définir")
    parts.append("📍 " + (game["location"] if game["location"] else "Lieu à définir"))
    return " · ".join(parts)


def update_schedule(game_id, date_value, time_value, location):
    execute(
        "UPDATE games SET event_date = ?, event_time = ?, location = ? WHERE id = ?",
        (date_value.isoformat() if date_value else None,
         time_value.strftime("%H:%M") if date_value and time_value else None,
         location.strip(), game_id),
    )


def schedule_defaults(game=None):
    if game and game["event_date"]:
        d = datetime.date.fromisoformat(game["event_date"])
        t = (datetime.time.fromisoformat(game["event_time"])
             if game["event_time"] else datetime.time(18, 0))
        return d, t, False
    return datetime.date.today(), datetime.time(18, 0), bool(game)


# ----------------------------------------------------------------------------
# Notifications par e-mail
# ----------------------------------------------------------------------------
def smtp_config():
    host = get_setting("SMTP_HOST", "")
    if not host:
        return None
    user = str(get_setting("SMTP_USER", ""))
    return {
        "host": str(host),
        "port": int(get_setting("SMTP_PORT", 587)),
        "user": user,
        "password": str(get_setting("SMTP_PASSWORD", "")),
        "sender": str(get_setting("SMTP_FROM", user)),
    }


def send_emails(messages):
    """messages : liste de (destinataire, objet, corps). Retourne (envoyés, échecs, erreur)."""
    cfg = smtp_config()
    if not cfg:
        return 0, len(messages), "Service e-mail non configuré (SMTP_HOST manquant)."
    sent, failed, error = 0, 0, ""
    try:
        ctx = ssl.create_default_context()
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
            server.starttls(context=ctx)
        with server:
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            for to, subject, body in messages:
                try:
                    msg = EmailMessage()
                    msg["From"] = cfg["sender"]
                    msg["To"] = to
                    msg["Subject"] = subject
                    msg.set_content(body)
                    server.send_message(msg)
                    sent += 1
                except Exception as exc:  # un destinataire en échec n'arrête pas les autres
                    failed += 1
                    error = str(exc)
    except Exception as exc:
        failed = len(messages) - sent
        error = str(exc)
    return sent, failed, error


def notify(game_id, kind, messages):
    """Envoie les e-mails et garde une trace. Retourne (envoyés, échecs, erreur)."""
    if not messages:
        return 0, 0, ""
    sent, failed, error = send_emails(messages)
    execute(
        "INSERT INTO notifications (game_id, kind, subject, total, sent, failed, error, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (game_id, kind, messages[0][1], len(messages), sent, failed, error,
         datetime.datetime.now().isoformat(timespec="seconds")),
    )
    return sent, failed, error


def game_notes(game):
    """Consignes facultatives rédigées par l'organisateur."""
    return (game["signup_method"] or "").strip()


def signup_text(game):
    """Méthode pour participer : création de compte puis participation."""
    text = DEFAULT_SIGNUP_METHOD
    if game_notes(game):
        text += "\n\nConsignes de l'organisateur :\n" + game_notes(game)
    return text


def welcome_message(pseudo, email):
    subject = "[Arène] Bienvenue — ton compte est créé"
    body = (
        f"Bonjour {pseudo},\n\n"
        f"Ton compte Arène est bien créé avec l'adresse {email}.\n\n"
        "Tu peux maintenant te connecter, choisir une compétition et cliquer sur "
        "« Participer ». Tu seras prévenu par e-mail du planning et de tes matchs.\n\n"
        "À bientôt !\nL'équipe Arène"
    )
    return (email, subject, body)


def game_info_block(game):
    return (f"Jeu : {game['name']}\n"
            f"Format : {format_label(game)}\n"
            f"{format_schedule(game)}")


def confirmation_message(game, pseudo, email):
    subject = f"[Arène] Participation confirmée — {game['name']}"
    notes = game_notes(game)
    body = (
        f"Bonjour {pseudo},\n\n"
        f"Ta participation à la compétition « {game['name']} » est bien enregistrée.\n\n"
        f"{game_info_block(game)}\n\n"
        + (f"Consignes de l'organisateur :\n{notes}\n\n" if notes else "")
        + "Tu seras prévenu par e-mail en cas de changement de planning et dès que "
        "ton premier match sera connu.\n\n"
        "À bientôt !\nL'équipe Arène"
    )
    return (email, subject, body)


CODE_SUBJECTS = {
    "verify": "[Arène] Ton code de vérification",
    "reset": "[Arène] Réinitialisation de ton mot de passe",
    "admin": "[Arène] Code de connexion administrateur",
}
CODE_INTROS = {
    "verify": "Voici ton code pour activer ton compte Arène :",
    "reset": "Voici ton code pour choisir un nouveau mot de passe :",
    "admin": "Voici ton code de connexion à l'espace administrateur :",
}


def code_message(email, code, purpose):
    # Le code n'est que dans le corps : l'objet (journalisé) ne le contient jamais.
    body = (
        f"Bonjour,\n\n{CODE_INTROS[purpose]}\n\n    {code}\n\n"
        f"Ce code est valable {CODE_TTL_MINUTES} minutes. Si tu n'es pas à l'origine de "
        "cette demande, ignore simplement ce message.\n\nL'équipe Arène"
    )
    return (email, CODE_SUBJECTS[purpose], body)


def planning_messages(game, reminder=False):
    rows = query("SELECT pseudo, email FROM participants "
                 "WHERE game_id = ? AND email IS NOT NULL AND email != ''", (game["id"],))
    subject = f"[Arène] {'Rappel' if reminder else 'Planning mis à jour'} — {game['name']}"
    intro = (f"Petit rappel pour la compétition « {game['name']} »."
             if reminder else
             f"Le planning de la compétition « {game['name']} » a changé.")
    return [
        (r["email"], subject,
         f"Bonjour {r['pseudo']},\n\n{intro}\n\n"
         f"{'Planning' if reminder else 'Nouveau planning'} :\n{format_schedule(game)}"
         "\n\nL'équipe Arène")
        for r in rows
    ]


def notify_round(game_id, round_no):
    """Prévient chaque joueur de son match (ou de sa qualification d'office)."""
    game = get_game(game_id)
    matches = query("SELECT * FROM matches WHERE game_id = ? AND round = ? ORDER BY idx",
                    (game_id, round_no))
    is_final = any(m["is_final"] for m in matches)
    round_label = "Finale" if is_final else f"Tour {round_no}"
    subject = f"[Arène] {round_label} — {game['name']}"
    messages = []
    for m in matches:
        players = query(
            "SELECT p.pseudo, p.email FROM match_players mp "
            "JOIN participants p ON p.id = mp.participant_id WHERE mp.match_id = ?",
            (m["id"],))
        names = [p["pseudo"] for p in players]
        for p in players:
            if not p["email"]:
                continue
            if len(players) == 1 and not is_final:
                detail = "Tu es qualifié d'office pour le tour suivant."
            else:
                others = [n for n in names if n != p["pseudo"]]
                detail = (f"{'Ton adversaire' if len(others) == 1 else 'Tes adversaires'} : "
                          f"{', '.join(others)}")
            where = "Finale" if m["is_final"] else f"{round_label} — match {m['idx']}"
            body = (f"Bonjour {p['pseudo']},\n\n"
                    f"{where} de la compétition « {game['name']} ».\n{detail}\n\n"
                    f"{format_schedule(game)}\n\nBonne chance !\nL'équipe Arène")
            messages.append((p["email"], subject, body))
    notify(game_id, "tour", messages)


# ----------------------------------------------------------------------------
# Logique de compétition
# ----------------------------------------------------------------------------
def get_game(game_id):
    rows = query("SELECT * FROM games WHERE id = ?", (game_id,))
    return rows[0] if rows else None


# --- Comptes utilisateurs ---------------------------------------------------
def hash_password(password, salt=None, iterations=200_000):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def create_user(pseudo, email, password, verified=True):
    return execute(
        "INSERT INTO users (pseudo, email, password_hash, verified, created_at) "
        "VALUES (?,?,?,?,?)",
        (pseudo, email, hash_password(password), int(verified),
         datetime.datetime.now().isoformat(timespec="seconds")),
    )


def authenticate(email, password):
    rows = query("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    if rows and verify_password(password, rows[0]["password_hash"]):
        return rows[0]
    if not rows:
        hash_password(password)  # même durée de calcul si l'e-mail est inconnu
    return None


def is_participating(game_id, user_id):
    return bool(query("SELECT 1 FROM participants WHERE game_id = ? AND user_id = ?",
                      (game_id, user_id)))


def join_game(game, user):
    """Inscrit le compte connecté à la compétition. Retourne (ok, message d'erreur)."""
    if game["status"] != "inscriptions":
        return False, "Les inscriptions sont closes."
    maxp = game["max_players"]
    try:
        with closing(get_conn()) as conn:
            # Insertion atomique : la limite de places est vérifiée dans la même requête,
            # donc deux personnes ne peuvent pas prendre la dernière place en même temps.
            cur = conn.execute(
                "INSERT INTO participants (game_id, user_id, pseudo, email) "
                "SELECT ?, ?, ?, ? WHERE ? IS NULL OR "
                "(SELECT COUNT(*) FROM participants WHERE game_id = ?) < ?",
                (game["id"], user["id"], user["pseudo"], user["email"],
                 maxp, game["id"], maxp))
            conn.commit()
            if cur.rowcount == 0:
                return False, "Les inscriptions sont complètes."
    except sqlite3.IntegrityError:
        return False, "Tu participes déjà à cette compétition."
    return True, ""


def delete_user(user_id):
    """Supprime un compte : retire ses participations aux compétitions encore ouvertes."""
    execute("DELETE FROM participants WHERE user_id = ? AND game_id IN "
            "(SELECT id FROM games WHERE status = 'inscriptions')", (user_id,))
    execute("UPDATE participants SET user_id = NULL WHERE user_id = ?", (user_id,))
    execute("DELETE FROM users WHERE id = ?", (user_id,))


def set_password(user_id, new_password):
    execute("UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id))


# --- Codes e-mail et protection contre les essais répétés ---------------------
def _now():
    return datetime.datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def hash_code(email, purpose, code):
    return hashlib.sha256(f"{email}:{purpose}:{code}".encode("utf-8")).hexdigest()


def verification_enabled():
    return bool(smtp_config()) and \
        str(get_setting("REQUIRE_EMAIL_VERIFICATION", "true")).lower() != "false"


def admin_code_required():
    return bool(smtp_config()) and \
        str(get_setting("ADMIN_EMAIL_CODE", "true")).lower() != "false"


def issue_code(email, purpose):
    """Crée un code à 6 chiffres (stocké haché). Retourne (code, message d'erreur)."""
    email = email.strip().lower()
    rows = query("SELECT created_at FROM verification_codes WHERE email = ? AND purpose = ?",
                 (email, purpose))
    if rows:
        age = (_now() - datetime.datetime.fromisoformat(rows[0]["created_at"])).total_seconds()
        if age < CODE_COOLDOWN_SECONDS:
            return None, f"Patiente {int(CODE_COOLDOWN_SECONDS - age) + 1} s avant de " \
                         "demander un nouveau code."
    code = f"{secrets.randbelow(10 ** 6):06d}"
    execute("DELETE FROM verification_codes WHERE email = ? AND purpose = ?", (email, purpose))
    execute(
        "INSERT INTO verification_codes (email, purpose, code_hash, expires_at, attempts, "
        "created_at) VALUES (?,?,?,?,0,?)",
        (email, purpose, hash_code(email, purpose, code),
         _iso(_now() + datetime.timedelta(minutes=CODE_TTL_MINUTES)), _iso(_now())))
    return code, ""


def check_code(email, purpose, code):
    """Vérifie un code (usage unique, expire, 5 essais max). Retourne (ok, message)."""
    email = email.strip().lower()
    code = (code or "").strip().replace(" ", "")
    rows = query("SELECT * FROM verification_codes WHERE email = ? AND purpose = ?",
                 (email, purpose))
    if not rows:
        return False, "Aucun code actif : demande-en un nouveau."
    row = rows[0]
    if datetime.datetime.fromisoformat(row["expires_at"]) < _now():
        execute("DELETE FROM verification_codes WHERE id = ?", (row["id"],))
        return False, "Ce code a expiré : demande-en un nouveau."
    if row["attempts"] >= CODE_MAX_ATTEMPTS:
        # La ligne est conservée jusqu'à expiration : le délai entre deux envois
        # empêche d'enchaîner les codes pour deviner.
        return False, "Trop d'essais : attends quelques instants puis demande un nouveau code."
    if hmac.compare_digest(hash_code(email, purpose, code), row["code_hash"]):
        execute("DELETE FROM verification_codes WHERE id = ?", (row["id"],))
        return True, ""
    attempts = row["attempts"] + 1
    execute("UPDATE verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
    if attempts >= CODE_MAX_ATTEMPTS:
        return False, "Code incorrect. Trop d'essais : demande un nouveau code."
    return False, f"Code incorrect ({CODE_MAX_ATTEMPTS - attempts} essai(s) restant(s))."


def too_many_failures(key):
    since = _iso(_now() - datetime.timedelta(minutes=LOGIN_LOCK_MINUTES))
    n = query("SELECT COUNT(*) AS n FROM login_failures WHERE key = ? AND ts > ?",
              (key, since))[0]["n"]
    return n >= LOGIN_MAX_FAILURES


def record_failure(key):
    execute("DELETE FROM login_failures WHERE ts < ?",
            (_iso(_now() - datetime.timedelta(hours=1)),))
    execute("INSERT INTO login_failures (key, ts) VALUES (?,?)", (key, _iso(_now())))


def clear_failures(key):
    execute("DELETE FROM login_failures WHERE key = ?", (key,))


def try_login(email, password):
    """Retourne (compte, message d'erreur). Bloque après trop d'échecs."""
    email = email.strip().lower()
    key = f"user:{email}"
    if too_many_failures(key):
        return None, f"Trop de tentatives. Réessaie dans {LOGIN_LOCK_MINUTES} minutes."
    row = authenticate(email, password)
    if not row:
        record_failure(key)
        return None, "Adresse e-mail ou mot de passe incorrect."
    clear_failures(key)
    return row, ""


def purge_unverified():
    execute("DELETE FROM users WHERE verified = 0 AND created_at < ?",
            (_iso(_now() - datetime.timedelta(hours=UNVERIFIED_TTL_HOURS)),))


def my_status(game, user_id):
    """Phrase décrivant où en est un joueur dans une compétition."""
    p = query("SELECT id FROM participants WHERE game_id = ? AND user_id = ?",
              (game["id"], user_id))
    if not p:
        return ""
    pid = p[0]["id"]
    if game["status"] == "inscriptions":
        return "inscrit, en attente du lancement de la compétition."
    rows = query(
        "SELECT m.id, m.round, m.is_final, m.done FROM matches m "
        "JOIN match_players mp ON mp.match_id = m.id "
        "WHERE m.game_id = ? AND mp.participant_id = ? ORDER BY m.round DESC LIMIT 1",
        (game["id"], pid))
    if not rows:
        return ""
    m = rows[0]
    players = match_players(m["id"])
    others = [x["pseudo"] for x in players if x["participant_id"] != pid]
    label = "Finale" if m["is_final"] else f"Tour {m['round']}"
    if not m["done"]:
        return f"{label} à jouer" + (f" contre {', '.join(others)}." if others else ".")
    place = next(x["place"] for x in players if x["participant_id"] == pid)
    if m["is_final"]:
        return f"compétition terminée, tu finis {place}ᵉ" + (" 🏆" if place == 1 else "") + "."
    if m["round"] < game["current_round"]:
        return f"éliminé au tour {m['round']} (place {place})."
    return "qualifié d'office, en attente des autres matchs."


def _csv_safe(value):
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def participants_csv(parts):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Pseudo", "E-mail"])
    for p in parts:
        writer.writerow([_csv_safe(p["pseudo"]), _csv_safe(p["email"])])
    return "﻿" + buf.getvalue()  # BOM : accents corrects dans Excel


def leave_game(game_id, user_id):
    execute("DELETE FROM participants WHERE game_id = ? AND user_id = ?", (game_id, user_id))


def split_groups(player_ids, match_size):
    """Répartit les joueurs en groupes équilibrés (tailles à 1 d'écart max)."""
    players = list(player_ids)
    random.shuffle(players)
    if len(players) <= match_size:
        return [players], True  # finale
    n_groups = math.ceil(len(players) / match_size)
    groups = [[] for _ in range(n_groups)]
    for i, p in enumerate(players):
        groups[i % n_groups].append(p)
    return groups, False


def qualified_players(game, round_no):
    """Joueurs qualifiés à l'issue d'un tour terminé."""
    ids = []
    matches = query(
        "SELECT id FROM matches WHERE game_id = ? AND round = ? ORDER BY idx",
        (game["id"], round_no),
    )
    for m in matches:
        mps = query(
            "SELECT participant_id, place FROM match_players WHERE match_id = ? ORDER BY place",
            (m["id"],),
        )
        size = len(mps)
        k = size if size == 1 else min(game["qualifiers"], size - 1)
        ids.extend(mp["participant_id"] for mp in mps if mp["place"] <= k)
    return ids


def generate_next_round(game_id):
    game = get_game(game_id)
    r = game["current_round"]
    if r == 0:
        players = [p["id"] for p in query(
            "SELECT id FROM participants WHERE game_id = ?", (game_id,))]
    else:
        players = qualified_players(game, r)

    groups, is_final = split_groups(players, game["match_size"])
    new_round = r + 1
    for idx, group in enumerate(groups, start=1):
        bye = len(group) == 1 and not is_final
        match_id = execute(
            "INSERT INTO matches (game_id, round, idx, is_final, done) VALUES (?,?,?,?,?)",
            (game_id, new_round, idx, int(is_final), int(bye)),
        )
        for pid in group:
            execute(
                "INSERT INTO match_players (match_id, participant_id, place) VALUES (?,?,?)",
                (match_id, pid, 1 if bye else None),
            )
    execute("UPDATE games SET current_round = ?, status = 'en cours' WHERE id = ?",
            (new_round, game_id))
    try:
        notify_round(game_id, new_round)
    except Exception:
        pass  # un problème d'e-mail ne doit jamais bloquer le tournoi
    check_round_complete(game_id)


def check_round_complete(game_id):
    """Passe au tour suivant (ou termine la compétition) si le tour est fini."""
    game = get_game(game_id)
    r = game["current_round"]
    pending = query(
        "SELECT COUNT(*) AS n FROM matches WHERE game_id = ? AND round = ? AND done = 0",
        (game_id, r),
    )[0]["n"]
    if pending:
        return
    has_final = query(
        "SELECT COUNT(*) AS n FROM matches WHERE game_id = ? AND round = ? AND is_final = 1",
        (game_id, r),
    )[0]["n"]
    if has_final:
        execute("UPDATE games SET status = 'terminée' WHERE id = ?", (game_id,))
    else:
        generate_next_round(game_id)


def start_competition(game_id):
    generate_next_round(game_id)


def reset_competition(game_id):
    execute("DELETE FROM matches WHERE game_id = ?", (game_id,))
    execute("UPDATE games SET status = 'inscriptions', current_round = 0 WHERE id = ?",
            (game_id,))


def save_match_result(game_id, match_id, places):
    """places : {participant_id: place}"""
    for pid, place in places.items():
        execute("UPDATE match_players SET place = ? WHERE match_id = ? AND participant_id = ?",
                (place, match_id, pid))
    execute("UPDATE matches SET done = 1 WHERE id = ?", (match_id,))
    check_round_complete(game_id)


def match_players(match_id):
    return query(
        """SELECT mp.participant_id, mp.place, p.pseudo
           FROM match_players mp JOIN participants p ON p.id = mp.participant_id
           WHERE mp.match_id = ?
           ORDER BY CASE WHEN mp.place IS NULL THEN 1 ELSE 0 END, mp.place, p.pseudo""",
        (match_id,),
    )


def podium(game_id):
    final = query(
        "SELECT id FROM matches WHERE game_id = ? AND is_final = 1 AND done = 1", (game_id,))
    if not final:
        return []
    return [r["pseudo"] for r in match_players(final[0]["id"])]


def format_label(game):
    if game["match_size"] == 2:
        return "Duels 1 contre 1"
    return (f"Matchs à {game['match_size']} joueurs "
            f"({game['qualifiers']} qualifié(s) par match)")


STATUS_ICON = {"inscriptions": "🟢 Inscriptions ouvertes", "en cours": "🟠 En cours",
               "terminée": "🏁 Terminée"}


# ----------------------------------------------------------------------------
# Affichage
# ----------------------------------------------------------------------------
def flash(message=None, area="public"):
    """Mémorise un message à afficher après un rerun (dans l'onglet `area`), ou l'affiche."""
    key = f"flash_{area}"
    if message:
        st.session_state[key] = message
    elif key in st.session_state:
        st.success(st.session_state.pop(key))


def render_match(game, match, is_admin):
    players = match_players(match["id"])
    title = "🏆 Finale" if match["is_final"] else f"Match {match['idx']}"
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if match["done"]:
            if len(players) == 1:
                st.write(f"✅ {players[0]['pseudo']} — qualifié d'office")
                return
            for p in players:
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(p["place"], f"{p['place']}ᵉ")
                st.write(f"{medal} {p['pseudo']}")
        else:
            st.write(" vs ".join(p["pseudo"] for p in players) or "—")
            if not is_admin:
                st.caption("En attente du résultat")
                return
            with st.form(f"form_match_{match['id']}"):
                n = len(players)
                if n == 2:
                    winner = st.radio("Gagnant", [p["pseudo"] for p in players],
                                      key=f"win_{match['id']}", horizontal=True)
                    chosen = {p["participant_id"]: (1 if p["pseudo"] == winner else 2)
                              for p in players}
                else:
                    chosen = {}
                    for i, p in enumerate(players):
                        chosen[p["participant_id"]] = st.selectbox(
                            f"Place de {p['pseudo']}", list(range(1, n + 1)), index=i,
                            key=f"place_{match['id']}_{p['participant_id']}")
                submitted = st.form_submit_button("Valider le résultat")
            if submitted:
                if sorted(chosen.values()) != list(range(1, n + 1)):
                    st.error("Chaque place (1 à %d) doit être attribuée une seule fois." % n)
                else:
                    save_match_result(game["id"], match["id"], chosen)
                    st.rerun()


def render_rounds(game, is_admin):
    matches = query(
        "SELECT * FROM matches WHERE game_id = ? ORDER BY round, idx", (game["id"],))
    if not matches:
        return
    rounds = {}
    for m in matches:
        rounds.setdefault(m["round"], []).append(m)
    for r in sorted(rounds, reverse=True):
        ms = rounds[r]
        label = "🏆 Finale" if any(m["is_final"] for m in ms) else f"Tour {r}"
        with st.expander(label, expanded=(r == game["current_round"])):
            cols = st.columns(min(3, len(ms)))
            for i, m in enumerate(ms):
                with cols[i % len(cols)]:
                    render_match(game, m, is_admin)


def render_podium(game):
    if game["status"] != "terminée":
        return
    names = podium(game["id"])
    if names:
        st.success(f"🏆 Champion : **{names[0]}**")
        if len(names) > 1:
            st.write("Classement final : " + " · ".join(
                f"{['🥇', '🥈', '🥉'][i] if i < 3 else str(i + 1) + 'ᵉ'} {n}"
                for i, n in enumerate(names)))


def render_game_header(game):
    count = query("SELECT COUNT(*) AS n FROM participants WHERE game_id = ?",
                  (game["id"],))[0]["n"]
    st.subheader(f"🎮 {game['name']}")
    st.caption(f"{STATUS_ICON.get(game['status'], game['status'])} · "
               f"{format_label(game)} · {count} participant(s)"
               + (f" / {game['max_players']}" if game["max_players"] else ""))
    st.markdown(f"**{format_schedule(game)}**")
    if game["status"] == "inscriptions":
        st.info("📝 **Comment participer**  \n" + signup_text(game).replace("\n", "  \n"))
    if game["description"]:
        st.write(game["description"])
    return count


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
def page_public():
    flash()
    games = query("SELECT * FROM games ORDER BY id DESC")
    if not games:
        st.info("Aucune compétition pour le moment. Revenez bientôt !")
        return
    labels = {g["id"]: f"{g['name']} — {g['status']}" for g in games}
    game_id = st.selectbox("Choisir une compétition", list(labels),
                           format_func=lambda i: labels[i])
    game = get_game(game_id)
    count = render_game_header(game)

    if game["status"] == "inscriptions":
        user = current_user()
        full = game["max_players"] and count >= game["max_players"]
        if user and is_participating(game_id, user["id"]):
            st.success(f"✅ Tu participes à cette compétition, {user['pseudo']}.")
            if st.button("Me retirer de la compétition", key=f"leave_{game_id}"):
                leave_game(game_id, user["id"])
                flash("Tu ne participes plus à cette compétition.")
                st.rerun()
        elif full:
            st.warning("Les inscriptions sont complètes.")
        elif not user:
            st.warning("Pour participer, crée un compte ou connecte-toi "
                       "dans l'onglet « Mon compte ».")
        elif st.button("✅ Participer à cette compétition", type="primary",
                       key=f"join_{game_id}"):
            ok, error = join_game(game, user)
            if not ok:
                st.error(error)
            else:
                sent, _, _ = notify(game_id, "confirmation", [
                    confirmation_message(game, user["pseudo"], user["email"])])
                flash(f"C'est noté, {user['pseudo']} ! Tu participes à la compétition. "
                      + (f"Un e-mail de confirmation a été envoyé à {user['email']}."
                         if sent else "(L'e-mail de confirmation n'a pas pu être envoyé.)"))
                st.rerun()

    if game["status"] != "inscriptions":
        user = current_user()
        status = my_status(game, user["id"]) if user else ""
        if status:
            st.info(f"🎯 Ton parcours : {status}")

    parts = query("SELECT pseudo FROM participants WHERE game_id = ? ORDER BY id", (game_id,))
    with st.expander(f"Participants ({len(parts)})"):
        st.write(", ".join(p["pseudo"] for p in parts) or "Personne pour l'instant.")
    render_podium(game)
    render_rounds(game, is_admin=False)


def current_user():
    user = st.session_state.get("user")
    if user and not query("SELECT 1 FROM users WHERE id = ?", (user["id"],)):
        st.session_state.pop("user", None)  # compte supprimé entre-temps
        return None
    return user


def login_user(row):
    st.session_state["user"] = {"id": row["id"], "pseudo": row["pseudo"], "email": row["email"]}


def send_code(email, purpose):
    """Génère un code et l'envoie par e-mail. Retourne (ok, message d'erreur)."""
    code, error = issue_code(email, purpose)
    if not code:
        return False, error
    sent, _, _ = notify(None, "code", [code_message(email, code, purpose)])
    if not sent:
        execute("DELETE FROM verification_codes WHERE email = ? AND purpose = ?",
                (email.strip().lower(), purpose))
        return False, "Impossible d'envoyer l'e-mail pour l'instant. Réessaie plus tard."
    return True, ""


def activate_and_login(row, message):
    """Marque le compte comme vérifié, connecte la personne et envoie le mail de bienvenue."""
    execute("UPDATE users SET verified = 1 WHERE id = ?", (row["id"],))
    st.session_state.pop("pending_verify", None)
    login_user(row)
    sent, _, _ = notify(None, "compte", [welcome_message(row["pseudo"], row["email"])])
    flash(message + (f" Un e-mail de bienvenue a été envoyé à {row['email']}." if sent else "")
          + " Va dans l'onglet « Compétitions » pour participer.", "account")
    st.rerun()


def render_verification(email):
    st.info(f"📧 Un code à 6 chiffres a été envoyé à **{email}**. "
            f"Il est valable {CODE_TTL_MINUTES} minutes. Pense à regarder les spams.")
    with st.form("user_verify"):
        code = st.text_input("Code de vérification")
        ok = st.form_submit_button("Activer mon compte")
    if ok:
        good, message = check_code(email, "verify", code)
        if good:
            rows = query("SELECT * FROM users WHERE email = ?", (email,))
            if rows:
                activate_and_login(rows[0], "Adresse vérifiée, compte activé !")
            else:
                st.session_state.pop("pending_verify", None)
                st.error("Ce compte n'existe plus, recommence l'inscription.")
        else:
            st.error(message)
    c1, c2 = st.columns(2)
    if c1.button("Renvoyer le code", key="resend_code"):
        good, message = send_code(email, "verify")
        if good:
            st.success("Un nouveau code vient d'être envoyé.")
        else:
            st.error(message)
    if c2.button("Annuler", key="cancel_verify"):
        st.session_state.pop("pending_verify", None)
        rerun_fragment()


def password_error(pwd, pwd2):
    if len(pwd) < MIN_PASSWORD_LENGTH:
        return f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères."
    if pwd != pwd2:
        return "Les deux mots de passe ne sont pas identiques."
    return ""


@fragment
def page_account():
    flash(area="account")
    user = current_user()
    if user:
        st.success(f"Connecté en tant que **{user['pseudo']}** ({user['email']})")
        mine = query(
            "SELECT g.* FROM participants p JOIN games g ON g.id = p.game_id "
            "WHERE p.user_id = ? ORDER BY g.id DESC", (user["id"],))
        st.markdown("**Mes compétitions**")
        if not mine:
            st.write("Tu ne participes à aucune compétition pour l'instant. "
                     "Va dans l'onglet « Compétitions » pour en rejoindre une.")
        for g in mine:
            status = my_status(g, user["id"])
            st.write(f"🎮 **{g['name']}** — {STATUS_ICON.get(g['status'], g['status'])}"
                     f" — {format_schedule(g)}" + (f"  \n🎯 {status}" if status else ""))
        with st.expander("🔑 Changer mon mot de passe"):
            with st.form("user_change_pwd"):
                old = st.text_input("Mot de passe actuel", type="password")
                new = st.text_input(
                    f"Nouveau mot de passe ({MIN_PASSWORD_LENGTH} caractères minimum)",
                    type="password")
                new2 = st.text_input("Confirmer le nouveau mot de passe", type="password")
                ok = st.form_submit_button("Changer le mot de passe")
            if ok:
                row, error = try_login(user["email"], old)
                if not row:
                    st.error(error.replace("Adresse e-mail ou mot de passe incorrect.",
                                           "Mot de passe actuel incorrect."))
                elif password_error(new, new2):
                    st.error(password_error(new, new2))
                else:
                    set_password(user["id"], new)
                    st.success("Mot de passe modifié.")
        if st.button("Se déconnecter", key="user_logout"):
            st.session_state.pop("user", None)
            st.rerun()
        return

    pending = st.session_state.get("pending_verify")
    if pending:
        render_verification(pending)
        return

    tab_login, tab_create, tab_forgot = st.tabs(
        ["Se connecter", "Créer un compte", "Mot de passe oublié"])

    with tab_login:
        with st.form("user_login"):
            email = st.text_input("Adresse e-mail")
            pwd = st.text_input("Mot de passe", type="password")
            ok = st.form_submit_button("Connexion")
        if ok:
            row, error = try_login(email, pwd)
            if not row:
                st.error(error)
            elif not row["verified"]:
                if verification_enabled():
                    send_code(row["email"], "verify")  # un code déjà envoyé reste valable
                    st.session_state["pending_verify"] = row["email"]
                    rerun_fragment()
                else:
                    activate_and_login(row, f"Bienvenue, {row['pseudo']} !")
            else:
                login_user(row)
                flash(f"Bienvenue, {row['pseudo']} ! Va dans l'onglet « Compétitions » "
                      "pour participer.", "account")
                st.rerun()

    with tab_create:
        with st.form("user_create"):
            pseudo = st.text_input("Pseudo *")
            email = st.text_input("Adresse e-mail *")
            pwd = st.text_input(
                f"Mot de passe * ({MIN_PASSWORD_LENGTH} caractères minimum)", type="password")
            pwd2 = st.text_input("Confirmer le mot de passe *", type="password")
            ok = st.form_submit_button("Créer mon compte")
        if ok:
            pseudo = pseudo.strip()
            email = email.strip().lower()
            purge_unverified()
            existing = query("SELECT * FROM users WHERE email = ?", (email,))
            if not pseudo or not email or not pwd:
                st.error("Le pseudo, l'adresse e-mail et le mot de passe sont obligatoires.")
            elif not valid_email(email):
                st.error("Adresse e-mail invalide.")
            elif password_error(pwd, pwd2):
                st.error(password_error(pwd, pwd2))
            elif existing and existing[0]["verified"]:
                st.error("Un compte existe déjà avec cette adresse e-mail. Connecte-toi.")
            elif query("SELECT 1 FROM users WHERE LOWER(pseudo) = LOWER(?) AND email != ?",
                       (pseudo, email)):
                st.error("Ce pseudo est déjà pris, choisis-en un autre.")
            else:
                needs_code = verification_enabled()
                try:
                    if existing:  # inscription jamais confirmée : on la remplace
                        execute("UPDATE users SET pseudo = ?, password_hash = ?, verified = ?, "
                                "created_at = ? WHERE email = ?",
                                (pseudo, hash_password(pwd), int(not needs_code),
                                 _iso(_now()), email))
                    else:
                        create_user(pseudo, email, pwd, verified=not needs_code)
                except sqlite3.IntegrityError:
                    st.error("Création impossible : pseudo ou e-mail déjà utilisé.")
                else:
                    row = query("SELECT * FROM users WHERE email = ?", (email,))[0]
                    if not needs_code:
                        activate_and_login(row, f"Compte créé, bienvenue {pseudo} !")
                    else:
                        sent_ok, message = send_code(email, "verify")
                        if sent_ok:
                            st.session_state["pending_verify"] = email
                            rerun_fragment()
                        else:
                            execute("DELETE FROM users WHERE email = ? AND verified = 0",
                                    (email,))
                            st.error(message)

    with tab_forgot:
        if not verification_enabled():
            st.info("La réinitialisation par e-mail n'est pas disponible pour le moment. "
                    "Contacte un administrateur.")
        else:
            with st.form("user_forgot"):
                email = st.text_input("Adresse e-mail du compte")
                ok = st.form_submit_button("M'envoyer un code")
            if ok:
                email = email.strip().lower()
                if query("SELECT 1 FROM users WHERE email = ? AND verified = 1", (email,)):
                    send_code(email, "reset")
                # Même message dans tous les cas : on ne révèle pas quels comptes existent.
                st.success("Si un compte existe avec cette adresse, un code vient d'être "
                           "envoyé. Entre-le ci-dessous avec ton nouveau mot de passe.")
            with st.form("user_reset"):
                email = st.text_input("E-mail du compte")
                code = st.text_input("Code reçu par e-mail (réinitialisation)")
                new = st.text_input(
                    f"Nouveau mot de passe ({MIN_PASSWORD_LENGTH} caractères minimum)",
                    type="password")
                new2 = st.text_input("Confirmer le nouveau mot de passe", type="password")
                ok = st.form_submit_button("Changer mon mot de passe")
            if ok:
                email = email.strip().lower()
                rows = query("SELECT * FROM users WHERE email = ? AND verified = 1", (email,))
                if password_error(new, new2):
                    st.error(password_error(new, new2))
                else:
                    good, message = check_code(email, "reset", code)
                    if good and rows:
                        set_password(rows[0]["id"], new)
                        clear_failures(f"user:{email}")
                        st.success("Mot de passe modifié. Tu peux te connecter.")
                    else:
                        st.error(message if not good else "Code invalide.")


@fragment
def admin_login():
    st.info("Accès réservé aux administrateurs.")
    pending = st.session_state.get("admin_pending")
    if pending:
        st.info(f"📧 Un code de connexion a été envoyé à **{pending}** "
                f"(valable {CODE_TTL_MINUTES} minutes).")
        with st.form("admin_code"):
            code = st.text_input("Code de connexion admin (reçu par e-mail)")
            ok = st.form_submit_button("Valider")
        if ok:
            good, message = check_code(pending, "admin", code)
            if good:
                st.session_state["admin"] = True
                st.session_state["admin_email"] = pending
                st.session_state.pop("admin_pending", None)
                st.rerun()
            else:
                st.error(message)
        if st.button("Annuler", key="admin_cancel"):
            st.session_state.pop("admin_pending", None)
            rerun_fragment()
        return

    with st.form("login"):
        email = st.text_input("Adresse e-mail admin")
        pwd = st.text_input("Mot de passe admin", type="password")
        ok = st.form_submit_button("Connexion admin")
    if ok:
        email = email.strip().lower()
        key = f"admin:{email}"
        expected = admin_password()
        if too_many_failures(key):
            st.error(f"Trop de tentatives. Réessaie dans {LOGIN_LOCK_MINUTES} minutes.")
        elif email in admin_emails() and hmac.compare_digest(pwd.encode(), expected.encode()):
            clear_failures(key)
            if admin_code_required():
                sent_ok, message = send_code(email, "admin")
                if sent_ok:
                    st.session_state["admin_pending"] = email
                    rerun_fragment()
                else:
                    st.error(message + " (Pour désactiver ce code : ADMIN_EMAIL_CODE=false.)")
            else:
                st.session_state["admin"] = True
                st.session_state["admin_email"] = email
                st.rerun()
        else:
            record_failure(key)
            st.error("Adresse e-mail ou mot de passe incorrect.")


def schedule_form_fields(game=None):
    d, t, undefined = schedule_defaults(game)
    k = f"sched_{game['id']}" if game else "sched_new"  # clés uniques par formulaire
    c1, c2 = st.columns(2)
    date_value = c1.date_input("Jour", value=d, key=f"{k}_date")
    time_value = c2.time_input("Heure", value=t, key=f"{k}_time")
    location = st.text_input("Lieu", value=(game["location"] if game else "") or "",
                             placeholder="Ex : Salle informatique B12", key=f"{k}_place")
    tbd = st.checkbox("Jour et heure à définir plus tard", value=undefined, key=f"{k}_tbd")
    return (None if tbd else date_value), time_value, location


def page_admin():
    flash(area="admin")
    if not st.session_state.get("admin"):
        admin_login()
        return
    c1, c2 = st.columns([4, 1])
    c1.caption(f"Connecté : {st.session_state.get('admin_email', '')}")
    if c2.button("Se déconnecter"):
        st.session_state["admin"] = False
        st.session_state.pop("admin_email", None)
        st.rerun()

    if not smtp_config():
        st.warning("⚠️ Les e-mails sont désactivés : aucun serveur SMTP n'est configuré "
                   "(SMTP_HOST, SMTP_USER, SMTP_PASSWORD). Les comptes sont créés sans "
                   "vérification d'e-mail, le mot de passe oublié est indisponible et "
                   "personne ne reçoit de notification.")
    if admin_password() == DEFAULT_ADMIN_PASSWORD:
        st.warning("🔒 Le mot de passe admin est celui écrit dans le code. Pour qu'il ne "
                   "soit pas lisible dans le fichier, définis ADMIN_PASSWORD "
                   "(variable d'environnement ou st.secrets).")

    with st.expander("➕ Proposer un nouveau jeu", expanded=not query("SELECT 1 FROM games")):
        with st.form("new_game", clear_on_submit=True):
            name = st.text_input("Nom du jeu")
            desc = st.text_area("Description / règles")
            size = st.number_input("Joueurs par match (2 = duel, 3 ou plus = multijoueur)",
                                   min_value=2, max_value=16, value=2)
            qual = st.number_input("Qualifiés par match (ignoré pour les duels)",
                                   min_value=1, max_value=15, value=1)
            maxp = st.number_input("Nombre max d'inscrits (0 = illimité)",
                                   min_value=0, value=0)
            date_value, time_value, location = schedule_form_fields()
            method = st.text_area(
                "Consignes pour les participants (facultatif, affichées sur la page "
                "et dans l'e-mail de confirmation)",
                placeholder="Ex : Viens 15 min avant le début avec ta manette.")
            ok = st.form_submit_button("Créer la compétition")
        if ok:
            if not name.strip():
                st.error("Le nom est obligatoire.")
            elif size > 2 and qual >= size:
                st.error("Les qualifiés par match doivent être inférieurs aux joueurs par match.")
            else:
                gid = execute(
                    "INSERT INTO games (name, description, match_size, qualifiers, max_players, "
                    "signup_method) VALUES (?,?,?,?,?,?)",
                    (name.strip(), desc.strip(), int(size),
                     1 if size == 2 else int(qual), int(maxp) or None, method.strip()),
                )
                update_schedule(gid, date_value, time_value, location)
                flash("Compétition créée.", "admin")
                st.rerun()

    games = query("SELECT * FROM games ORDER BY id DESC")
    if not games:
        return
    labels = {g["id"]: f"{g['name']} — {g['status']}" for g in games}
    game_id = st.selectbox("Gérer une compétition", list(labels),
                           format_func=lambda i: labels[i], key="admin_game")
    game = get_game(game_id)
    count = render_game_header(game)

    with st.expander("🗓️ Modifier le jour, l'heure, le lieu et les consignes"):
        with st.form(f"schedule_{game_id}"):
            date_value, time_value, location = schedule_form_fields(game)
            method = st.text_area("Consignes pour les participants (facultatif)",
                                  value=game["signup_method"] or "")
            warn = st.checkbox("Prévenir les inscrits par e-mail si le planning change",
                               value=True)
            saved = st.form_submit_button("Enregistrer")
        if saved:
            before = format_schedule(game)
            update_schedule(game_id, date_value, time_value, location)
            execute("UPDATE games SET signup_method = ? WHERE id = ?",
                    (method.strip(), game_id))
            updated = get_game(game_id)
            msg = "Modifications enregistrées."
            if warn and format_schedule(updated) != before:
                sent, failed, _ = notify(game_id, "planning", planning_messages(updated))
                if sent or failed:
                    msg += f" E-mails envoyés : {sent}" + (
                        f" (échecs : {failed})" if failed else "") + "."
            flash(msg, "admin")
            st.rerun()

    parts = query("SELECT * FROM participants WHERE game_id = ? ORDER BY id", (game_id,))

    with st.expander("📣 Envoyer une annonce aux inscrits"):
        with st.form(f"announce_{game_id}", clear_on_submit=True):
            subject = st.text_input("Objet")
            body = st.text_area("Message")
            go = st.form_submit_button("Envoyer à tous les inscrits")
        if go:
            targets = [p for p in parts if p["email"]]
            if not subject.strip() or not body.strip():
                st.error("L'objet et le message sont obligatoires.")
            elif not targets:
                st.error("Aucun inscrit avec une adresse e-mail.")
            else:
                sent, failed, err = notify(game_id, "annonce", [
                    (p["email"], f"[Arène] {subject.strip()} — {game['name']}",
                     f"Bonjour {p['pseudo']},\n\n{body.strip()}\n\nL'équipe Arène")
                    for p in targets])
                flash(f"Annonce envoyée à {sent} personne(s)"
                      + (f" — {failed} échec(s) : {err}" if failed else "."), "admin")
                st.rerun()

    with st.expander("📬 Historique des notifications"):
        logs = query("SELECT * FROM notifications WHERE game_id = ? OR game_id IS NULL "
                     "ORDER BY id DESC LIMIT 30", (game_id,))
        if not logs:
            st.write("Aucune notification envoyée pour l'instant.")
        for r in logs:
            icon = "✅" if not r["failed"] else "⚠️"
            st.write(f"{icon} {r['created_at']} · {r['kind']} · {r['subject']} · "
                     f"{r['sent']}/{r['total']} envoyé(s)"
                     + (f" — {r['error']}" if r["failed"] and r["error"] else ""))

    c_remind, c_csv = st.columns(2)
    if c_remind.button("🔔 Envoyer un rappel du planning", key=f"remind_{game_id}"):
        sent, failed, err = notify(game_id, "rappel", planning_messages(game, reminder=True))
        if sent or failed:
            flash(f"Rappel envoyé à {sent} personne(s)"
                  + (f" — {failed} échec(s) : {err}" if failed else "."), "admin")
        else:
            flash("Aucun participant à prévenir.", "admin")
        st.rerun()
    c_csv.download_button("⬇️ Exporter les participants (CSV)", data=participants_csv(parts),
                          file_name=f"participants-{game_id}.csv", mime="text/csv",
                          key=f"csv_{game_id}")

    with st.expander("👥 Comptes utilisateurs"):
        users = query("SELECT * FROM users ORDER BY id DESC LIMIT 200")
        st.caption(f"{len(users)} compte(s) affiché(s)")
        for u in users:
            a, b = st.columns([5, 1])
            a.write(f"{u['pseudo']} — {u['email']}" + ("" if u["verified"] else " (non vérifié)"))
            if b.button("🗑️", key=f"del_user_{u['id']}"):
                delete_user(u["id"])
                flash("Compte supprimé.", "admin")
                st.rerun()

    if game["status"] == "inscriptions":
        st.markdown("**Participants inscrits**")
        for p in parts:
            c1, c2 = st.columns([5, 1])
            c1.write(f"{p['pseudo']} — {p['email'] or 'e-mail non renseigné'}")
            if c2.button("🗑️", key=f"del_p_{p['id']}"):
                execute("DELETE FROM participants WHERE id = ?", (p["id"],))
                st.rerun()
        if count < 2:
            st.info("Il faut au moins 2 participants pour lancer la compétition.")
        elif st.button("🚀 Clôturer les inscriptions et générer le tournoi", type="primary"):
            start_competition(game_id)
            st.rerun()
    else:
        with st.expander(f"Participants et e-mails ({len(parts)})"):
            for p in parts:
                st.write(f"{p['pseudo']} — {p['email'] or 'e-mail non renseigné'}")
        render_podium(game)
        render_rounds(game, is_admin=True)
        if st.button("↩️ Réinitialiser (efface les matchs, rouvre les inscriptions)"):
            reset_competition(game_id)
            st.rerun()

    st.divider()
    if st.checkbox("Afficher la suppression de la compétition", key=f"conf_{game_id}"):
        if st.button("❌ Supprimer définitivement cette compétition"):
            execute("DELETE FROM games WHERE id = ?", (game_id,))
            st.rerun()


# ----------------------------------------------------------------------------
init_db()
st.title("🎮 Arène — compétitions de jeux vidéo")
tab_public, tab_account, tab_admin = st.tabs(["Compétitions", "👤 Mon compte", "Admin"])
with tab_public:
    page_public()
with tab_account:
    page_account()
with tab_admin:
    page_admin()
