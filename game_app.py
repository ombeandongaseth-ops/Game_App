"""
Arène — gestion de compétitions de jeux vidéo avec Streamlit.

- L'admin propose un jeu (joueurs par match, qualifiés par match, date, heure, lieu).
- Les participants s'inscrivent avec un pseudo ET une adresse e-mail
  (une même adresse ne peut s'inscrire qu'une seule fois par compétition).
- L'admin lance la compétition : l'app génère les tours d'élimination.
  * Jeu en duel (2 joueurs par match) : 1 gagnant qualifié par match.
  * Jeu à 3 joueurs ou plus : les joueurs sont répartis en groupes de la
    taille du jeu, les meilleurs de chaque match se qualifient.
- Le dernier tour (finale) réunit tous les joueurs restants dans un seul match.
- L'accès admin exige l'adresse e-mail d'un admin + le mot de passe admin.

Lancement : streamlit run app.py

Configuration (variables d'environnement ou st.secrets) :
  ADMIN_PASSWORD : mot de passe admin (par défaut : "admin", À CHANGER)
  ADMIN_EMAILS   : adresses admin séparées par des virgules
                   (par défaut : ninon.ombeandonga@2030.ucac-icam.com)
"""

import datetime
import math
import os
import random
import re
import sqlite3
from contextlib import closing

import streamlit as st

DB_PATH = os.environ.get("ARENA_DB", "arena.db")
DEFAULT_ADMIN_EMAILS = "ninon.ombeandonga@2030.ucac-icam.com"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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


# ----------------------------------------------------------------------------
# Base de données
# ----------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
                location TEXT DEFAULT ''
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
            """
        )
        # Migration des bases créées avec une version précédente
        ensure_column(conn, "games", "event_date", "TEXT")
        ensure_column(conn, "games", "event_time", "TEXT")
        ensure_column(conn, "games", "location", "TEXT DEFAULT ''")
        ensure_column(conn, "participants", "email", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_participant_email "
            "ON participants(game_id, email)"
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
# Logique de compétition
# ----------------------------------------------------------------------------
def get_game(game_id):
    rows = query("SELECT * FROM games WHERE id = ?", (game_id,))
    return rows[0] if rows else None


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
def flash(message=None):
    """Mémorise un message à afficher après un rerun, ou l'affiche."""
    if message:
        st.session_state["flash"] = message
    elif "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))


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
               f"{format_label(game)} · {count} inscrit(s)"
               + (f" / {game['max_players']}" if game["max_players"] else ""))
    st.markdown(f"**{format_schedule(game)}**")
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
        full = game["max_players"] and count >= game["max_players"]
        if full:
            st.warning("Les inscriptions sont complètes.")
        else:
            with st.form(f"register_{game_id}", clear_on_submit=True):
                pseudo = st.text_input("Ton pseudo *")
                email = st.text_input("Ton adresse e-mail *")
                ok = st.form_submit_button("S'inscrire")
            if ok:
                pseudo = pseudo.strip()
                email = email.strip().lower()
                if not pseudo or not email:
                    st.error("Le pseudo et l'adresse e-mail sont obligatoires.")
                elif not valid_email(email):
                    st.error("Adresse e-mail invalide.")
                elif query("SELECT 1 FROM participants WHERE game_id = ? AND email = ?",
                           (game_id, email)):
                    st.error("Cette adresse e-mail est déjà inscrite à cette compétition. "
                             "Une personne ne peut s'inscrire qu'une seule fois.")
                elif query("SELECT 1 FROM participants WHERE game_id = ? "
                           "AND LOWER(pseudo) = LOWER(?)", (game_id, pseudo)):
                    st.error("Ce pseudo est déjà pris pour cette compétition.")
                else:
                    try:
                        execute("INSERT INTO participants (game_id, pseudo, email) "
                                "VALUES (?,?,?)", (game_id, pseudo, email))
                        flash(f"Bienvenue, {pseudo} ! Tu es inscrit.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Inscription impossible : pseudo ou e-mail déjà utilisé.")

    parts = query("SELECT pseudo FROM participants WHERE game_id = ? ORDER BY id", (game_id,))
    with st.expander(f"Participants ({len(parts)})"):
        st.write(", ".join(p["pseudo"] for p in parts) or "Personne pour l'instant.")
    render_podium(game)
    render_rounds(game, is_admin=False)


def admin_login():
    st.info("Accès réservé aux administrateurs.")
    with st.form("login"):
        email = st.text_input("Adresse e-mail admin")
        pwd = st.text_input("Mot de passe admin", type="password")
        ok = st.form_submit_button("Connexion")
    if ok:
        expected = str(get_setting("ADMIN_PASSWORD", "admin"))
        if email.strip().lower() in admin_emails() and pwd == expected:
            st.session_state["admin"] = True
            st.session_state["admin_email"] = email.strip().lower()
            st.rerun()
        else:
            st.error("Adresse e-mail ou mot de passe incorrect.")


def schedule_form_fields(game=None):
    d, t, undefined = schedule_defaults(game)
    c1, c2 = st.columns(2)
    date_value = c1.date_input("Jour", value=d)
    time_value = c2.time_input("Heure", value=t)
    location = st.text_input("Lieu", value=(game["location"] if game else "") or "",
                             placeholder="Ex : Salle informatique B12")
    tbd = st.checkbox("Jour et heure à définir plus tard", value=undefined)
    return (None if tbd else date_value), time_value, location


def page_admin():
    flash()
    if not st.session_state.get("admin"):
        admin_login()
        return
    c1, c2 = st.columns([4, 1])
    c1.caption(f"Connecté : {st.session_state.get('admin_email', '')}")
    if c2.button("Se déconnecter"):
        st.session_state["admin"] = False
        st.session_state.pop("admin_email", None)
        st.rerun()

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
            ok = st.form_submit_button("Créer la compétition")
        if ok:
            if not name.strip():
                st.error("Le nom est obligatoire.")
            elif size > 2 and qual >= size:
                st.error("Les qualifiés par match doivent être inférieurs aux joueurs par match.")
            else:
                gid = execute(
                    "INSERT INTO games (name, description, match_size, qualifiers, max_players) "
                    "VALUES (?,?,?,?,?)",
                    (name.strip(), desc.strip(), int(size),
                     1 if size == 2 else int(qual), int(maxp) or None),
                )
                update_schedule(gid, date_value, time_value, location)
                flash("Compétition créée.")
                st.rerun()

    games = query("SELECT * FROM games ORDER BY id DESC")
    if not games:
        return
    labels = {g["id"]: f"{g['name']} — {g['status']}" for g in games}
    game_id = st.selectbox("Gérer une compétition", list(labels),
                           format_func=lambda i: labels[i], key="admin_game")
    game = get_game(game_id)
    count = render_game_header(game)

    with st.expander("🗓️ Modifier le jour, l'heure et le lieu"):
        with st.form(f"schedule_{game_id}"):
            date_value, time_value, location = schedule_form_fields(game)
            saved = st.form_submit_button("Enregistrer le planning")
        if saved:
            update_schedule(game_id, date_value, time_value, location)
            flash("Planning mis à jour.")
            st.rerun()

    parts = query("SELECT * FROM participants WHERE game_id = ? ORDER BY id", (game_id,))

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
tab_public, tab_admin = st.tabs(["Compétitions", "Admin"])
with tab_public:
    page_public()
with tab_admin:
    page_admin()
