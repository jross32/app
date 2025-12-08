


import itertools
import json
import os
import threading
import logging
import random
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, abort, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import json as pyjson
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Text, DateTime, ForeignKey, func, or_
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cue-ligans-secret')
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'letmein')
ALLOWED_EXTENSIONS = {'json'}
LOCK = threading.Lock()
# Default: short session (6 hours). If "Remember this device" is checked, we keep permanent (14 days via cookie duration set elsewhere).
app.permanent_session_lifetime = timedelta(hours=6)

# League data configuration (new unified JSON)
# Primary league dataset (legacy week 11 snapshot)
LEAGUE_DATA_FILENAME = 'league_data_final_week11.json'
LOCKER_DB = os.path.join(DATA_DIR, 'locker.db')
TEAM_LOGO_DIR = Path(app.static_folder) / 'img' / 'team_logos'
DEFAULT_TEAM_LOGO_URL = '/static/img/team_logos/cue-ligans-logo.png'
TEAM_LOGO_EXTENSIONS = ('png', 'svg', 'jpg', 'jpeg')

# Helper to normalize team names (for matching 'Cue-ligans' vs 'Cue Ligans', etc.)
def normalize_team_name(name: str) -> str:
    return ''.join(ch for ch in str(name).lower() if ch.isalnum())

# Normalized name for our team (Cüe-Ligans)
MY_TEAM_KEY = normalize_team_name('Cue-ligans')

# --- Database setup for Locker ---
engine = create_engine(f"sqlite:///{LOCKER_DB}", connect_args={"check_same_thread": False})
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False))
Base = declarative_base()


class Role(Base):
    __tablename__ = 'roles'
    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    permissions = Column(Text)  # JSON string for future use


class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, ForeignKey('users.id'))
    action = Column(String(120))
    target = Column(String(120))
    detail = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    date = Column(DateTime)
    location = Column(String(200))
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Badge(Base):
    __tablename__ = 'badges'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(Text)
    icon = Column(String(120))


class UserBadge(Base):
    __tablename__ = 'user_badges'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), index=True)
    badge_id = Column(Integer, ForeignKey('badges.id'), index=True)
    awarded_at = Column(DateTime, default=datetime.utcnow)
    badge = relationship("Badge")

class AIThread(Base):
    __tablename__ = 'ai_threads'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), index=True, nullable=False)
    title = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_archived = Column(Boolean, default=False)
    user = relationship("User")
    chats = relationship("AIChat", back_populates="thread")

class AIChat(Base):
    __tablename__ = 'ai_chats'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), index=True, nullable=False)
    thread_id = Column(Integer, ForeignKey('ai_threads.id'), index=True, nullable=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")
    thread = relationship("AIThread", back_populates="chats")


class Channel(Base):
    __tablename__ = 'locker_channels'
    id = Column(Integer, primary_key=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(255))
    position = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    is_locked = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    messages = relationship("Message", back_populates="channel", order_by="Message.created_at")


class Message(Base):
    __tablename__ = 'locker_messages'
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey('locker_channels.id'), nullable=False, index=True)
    author_name = Column(String(120), default='Guest')
    author_initials = Column(String(4), default='CL')
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_system = Column(Boolean, default=False)
    media_url = Column(String(255))
    is_pinned = Column(Boolean, default=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    channel = relationship("Channel", back_populates="messages")
    user = relationship("User", back_populates="messages", lazy="joined")

class ProfileComment(Base):
    __tablename__ = 'profile_comments'
    id = Column(Integer, primary_key=True)
    profile_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    author_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    profile_user = relationship("User", foreign_keys=[profile_user_id])
    author = relationship("User", foreign_keys=[author_user_id])


class DirectMessage(Base):
    __tablename__ = 'direct_messages'
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey('users.id'), index=True, nullable=False)
    recipient_id = Column(Integer, ForeignKey('users.id'), index=True, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_read = Column(Boolean, default=False, index=True)
    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String(30), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(40), default='member')
    is_suspended = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    is_muted = Column(Boolean, default=False)
    memes_only = Column(Boolean, default=False)
    display_name = Column(String(120))
    initials = Column(String(4))
    player_ref = Column(String(120))
    team_id = Column(String(120))
    player_apa_id = Column(String(120))
    avatar_url = Column(String(255))
    nickname_history = Column(Text)  # JSON list of prior display names
    bio = Column(Text)
    about_me = Column(Text)
    interests = Column(Text)
    profile_accent_color = Column(String(20))
    profile_background_url = Column(String(255))
    profile_banner_url = Column(String(255))
    theme_song_url = Column(String(255))
    theme_song_title = Column(String(255))
    theme_song_artist = Column(String(255))
    favorite_quote = Column(Text)
    favorite_game = Column(String(40))
    current_mood = Column(String(80))
    created_at = Column(DateTime, default=datetime.utcnow)
    messages = relationship("Message", back_populates="user")
    comments_authored = relationship("ProfileComment", back_populates="author", foreign_keys="ProfileComment.author_user_id")
    comments_received = relationship("ProfileComment", back_populates="profile_user", foreign_keys="ProfileComment.profile_user_id")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash, password)
        except Exception:
            return False

    def add_nickname_history(self, prior: str):
        history = []
        if self.nickname_history:
            try:
                history = pyjson.loads(self.nickname_history)
            except Exception:
                history = []
        if prior and prior not in history:
            history.append(prior)
            self.nickname_history = pyjson.dumps(history)

    def get_nickname_history(self):
        if not self.nickname_history:
            return []
        try:
            return pyjson.loads(self.nickname_history)
        except Exception:
            return []


def init_locker():
    """Create tables and seed default channels if missing."""
    os.makedirs(DATA_DIR, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # Lightweight migration for new columns
    try:
        with engine.begin() as conn:
            cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info('locker_messages')")]
            if 'user_id' not in cols:
                conn.exec_driver_sql("ALTER TABLE locker_messages ADD COLUMN user_id INTEGER")
            if 'is_pinned' not in cols:
                conn.exec_driver_sql("ALTER TABLE locker_messages ADD COLUMN is_pinned BOOLEAN")
            user_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info('users')")]
            if 'role' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN role TEXT")
            if 'is_suspended' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_suspended BOOLEAN")
            if 'is_banned' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_banned BOOLEAN")
            if 'is_muted' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_muted BOOLEAN")
            if 'memes_only' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN memes_only BOOLEAN")
            if 'nickname_history' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN nickname_history TEXT")
            if 'player_ref' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN player_ref TEXT")
            if 'team_id' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN team_id TEXT")
            if 'player_apa_id' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN player_apa_id TEXT")
            if 'avatar_url' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN avatar_url TEXT")
            if 'about_me' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN about_me TEXT")
            if 'interests' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN interests TEXT")
            if 'profile_accent_color' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN profile_accent_color TEXT")
            if 'profile_background_url' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN profile_background_url TEXT")
            if 'profile_banner_url' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN profile_banner_url TEXT")
            if 'theme_song_url' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN theme_song_url TEXT")
            if 'theme_song_title' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN theme_song_title TEXT")
            if 'theme_song_artist' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN theme_song_artist TEXT")
            if 'favorite_quote' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN favorite_quote TEXT")
            if 'favorite_game' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN favorite_game TEXT")
            if 'current_mood' not in user_cols:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN current_mood TEXT")
            # locker_channels columns
            chan_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info('locker_channels')")]
            if 'is_locked' not in chan_cols:
                conn.exec_driver_sql("ALTER TABLE locker_channels ADD COLUMN is_locked BOOLEAN")
            if 'is_hidden' not in chan_cols:
                conn.exec_driver_sql("ALTER TABLE locker_channels ADD COLUMN is_hidden BOOLEAN")
            existing_tables = [row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")]
            if 'profile_comments' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE profile_comments (
                        id INTEGER PRIMARY KEY,
                        profile_user_id INTEGER NOT NULL,
                        author_user_id INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        created_at DATETIME,
                        FOREIGN KEY(profile_user_id) REFERENCES users(id),
                        FOREIGN KEY(author_user_id) REFERENCES users(id)
                    )
                """)
            if 'roles' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE roles (
                        id INTEGER PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        permissions TEXT
                    )
                """)
            if 'audit_logs' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE audit_logs (
                        id INTEGER PRIMARY KEY,
                        actor_user_id INTEGER,
                        action TEXT,
                        target TEXT,
                        detail TEXT,
                        created_at DATETIME
                    )
                """)
            if 'events' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY,
                        title TEXT,
                        date DATETIME,
                        location TEXT,
                        description TEXT,
                        created_at DATETIME
                    )
                """)
            if 'badges' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE badges (
                        id INTEGER PRIMARY KEY,
                        name TEXT UNIQUE,
                        description TEXT,
                        icon TEXT
                    )
                """)
            if 'user_badges' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE user_badges (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER,
                        badge_id INTEGER,
                        awarded_at DATETIME,
                        FOREIGN KEY(user_id) REFERENCES users(id),
                        FOREIGN KEY(badge_id) REFERENCES badges(id)
                    )
                """)
            if 'ai_threads' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE ai_threads (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        title TEXT,
                        created_at DATETIME,
                        updated_at DATETIME,
                        is_archived BOOLEAN DEFAULT 0,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                """)
            if 'ai_chats' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE ai_chats (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        thread_id INTEGER,
                        question TEXT NOT NULL,
                        answer TEXT,
                        created_at DATETIME,
                        FOREIGN KEY(user_id) REFERENCES users(id),
                        FOREIGN KEY(thread_id) REFERENCES ai_threads(id)
                    )
                """)
            ai_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info('ai_chats')")]
            if 'thread_id' not in ai_cols:
                conn.exec_driver_sql("ALTER TABLE ai_chats ADD COLUMN thread_id INTEGER")
            if 'direct_messages' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE direct_messages (
                        id INTEGER PRIMARY KEY,
                        sender_id INTEGER NOT NULL,
                        recipient_id INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        created_at DATETIME,
                        is_read BOOLEAN DEFAULT 0,
                        FOREIGN KEY(sender_id) REFERENCES users(id),
                        FOREIGN KEY(recipient_id) REFERENCES users(id)
                    )
                """)
            if 'direct_messages' not in existing_tables:
                conn.exec_driver_sql("""
                    CREATE TABLE direct_messages (
                        id INTEGER PRIMARY KEY,
                        sender_id INTEGER NOT NULL,
                        recipient_id INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        created_at DATETIME,
                        is_read BOOLEAN DEFAULT 0,
                        FOREIGN KEY(sender_id) REFERENCES users(id),
                        FOREIGN KEY(recipient_id) REFERENCES users(id)
                    )
                """)
            # Seed roles with empty permissions (editable later)
            seed_roles = ['admin','moderator','captain','trusted','member','rookie','vip','meme-king']
            for r in seed_roles:
                conn.exec_driver_sql("INSERT OR IGNORE INTO roles (name, permissions) VALUES (?, ?)", (r, pyjson.dumps([])))
            # Seed a handful of default badges used across UI/admin
            default_badges = [
                # Core / starter badges
                ("Locker Rookie", "Joined the app", "fa-star"),
                ("First Message", "Posted in Locker", "fa-message"),
                ("Memes Dealer", "Active in Memes & Media", "fa-face-grin-squint-tears"),
                ("Shot Caller", "Posted in Strategies", "fa-chess-knight"),
                ("Team OG", "Joined early", "fa-crown"),
                # Pool / APA skill badges
                ("Break Beast", "Known for big, explosive breaks.", "fa-bolt"),
                ("Clutch Shooter", "Wins when the team needs it most.", "fa-bullseye"),
                ("Clean Run", "Ran out a rack with no misses.", "fa-check-double"),
                ("Safety Ninja", "Plays sneaky safeties and lock-ups.", "fa-user-ninja"),
                ("Comeback King", "Turned a losing match into a win.", "fa-rotate-left"),
                # Locker activity badges
                ("Hype Master", "Brings energy in Locker before games.", "fa-bullhorn"),
                ("Media Dropper", "Shares clips and media in Locker.", "fa-play-circle"),
                ("Hyperactive", "Active in multiple Locker channels.", "fa-bolt-lightning"),
                ("Night Owl", "Posts in the middle of the night.", "fa-moon"),
                ("Early Bird", "Posts early in the morning.", "fa-sun"),
                # Profile / MySpace style badges
                ("Theme Lord", "Customized their profile theme.", "fa-palette"),
                ("Audiophile", "Set a profile theme song.", "fa-headphones"),
                # Social / fun badges
                ("Vibe Setter", "Sets the mood on match nights.", "fa-wand-magic-sparkles"),
                ("Clown Shoes", "Certified jokester of the squad.", "fa-face-laugh-beam"),
                ("Low-Key Menace", "Chaos energy, but in a fun way.", "fa-mask-face"),
                # Admin / season badges
                ("Captain", "Team captain role.", "fa-flag"),
                ("Co-Captain", "Assistant captain.", "fa-user-shield"),
                ("MVP", "Most valuable player of a session.", "fa-trophy"),
                ("Most Improved", "Made noticeable progress in play.", "fa-arrow-trend-up"),
                # New: Precision/skill
                ("Precision Shooter", "Dead-accurate aim with minimal misses.", "fa-crosshairs"),
                ("Kick Master", "Wins racks using clutch kick shots.", "fa-share"),
                ("Bank Shot Bandit", "Loves banking balls into pockets.", "fa-arrows-turn-to-dots"),
                ("Safety Engineer", "Advanced strategic safety setups.", "fa-shield-halved"),
                ("Break & Run", "Did a clean break-and-run in 8 or 9-ball.", "fa-bolt"),
                ("Runout Machine", "Multiple runouts in one session.", "fa-infinity"),
                ("Closer of the Night", "Won the final deciding match.", "fa-lock"),
                ("Rail Rider", "Expert at rail shots.", "fa-grip-lines"),
                ("Spin Doctor", "Uses crazy spin effectively.", "fa-fan"),
                ("Table General", "Controls the flow of the match.", "fa-chess-king"),
                # Locker & social
                ("Daily Grinder", "Logs into Locker every day.", "fa-calendar-check"),
                ("Keyboard Warrior", "Super active in discussion threads.", "fa-keyboard"),
                ("Uploader Pro", "Regularly uploads videos/photos.", "fa-upload"),
                ("Channel Hopper", "Uses every channel at least once.", "fa-table-cells"),
                ("Mood Setter", "Changes mood/status often.", "fa-face-smile-beam"),
                ("Deep Thinker", "Posts long thoughts or breakdowns.", "fa-brain"),
                ("Shot Predictor", "Predicts match outcomes and is correct.", "fa-crystal-ball"),
                ("Crowd Pleaser", "Messages often get positive reactions.", "fa-thumbs-up"),
                ("Firestarter", "Starts active conversations.", "fa-fire"),
                ("Smooth Talker", "Compliment king/queen.", "fa-comment-dots"),
                # Profile / customization
                ("Glow Architect", "Uses custom accent colors.", "fa-palette"),
                ("Wallpaper Wizard", "Custom profile background image.", "fa-image"),
                ("Design Freak", "Changes profile frequently.", "fa-sliders"),
                ("Playlist Curator", "Adds multiple theme songs over time.", "fa-music"),
                ("Bio Poet", "Writes meaningful or poetic bio.", "fa-feather"),
                ("Top Friends Royalty", "Appears on multiple users’ Top 4.", "fa-crown"),
                ("Secret Admirer", "Leaves lots of comments on profiles.", "fa-heart"),
                ("Comment Collector", "Has lots of comments on their wall.", "fa-comments"),
                ("Aesthetic Icon", "Profile is always clean and cohesive.", "fa-sparkles"),
                ("Flashy Flexer", "Profile is flashy, bold, loud.", "fa-bolt-lightning"),
                # Fun / personality
                ("Chaos Gremlin", "Pure chaotic energy.", "fa-face-grin-tongue-wink"),
                ("Silent Assassin", "Rarely speaks but always effective.", "fa-ghost"),
                ("Motivational Speaker", "Positive energy and uplifting vibes.", "fa-bullhorn"),
                ("Meme Surgeon", "Cuts deep with perfect memes.", "fa-masks-theater"),
                ("Drama-Free Zone", "Never gets involved in arguments.", "fa-peace"),
                ("The Philosopher", "Posts deep, thoughtful quotes.", "fa-book"),
                ("Story Mode", "Writes long stories and post recaps.", "fa-scroll"),
                ("The Ghoster", "Disappears for long periods.", "fa-user-slash"),
                ("The Return", "Comes back after a long break.", "fa-rotate-left"),
                ("Emoji Addict", "Uses a LOT of emojis.", "fa-face-grin-hearts"),
                # Team / season achievement
                ("Weeknight Warrior", "Never misses league night.", "fa-shield"),
                ("Hustle Heart", "Shows big effort each week.", "fa-heart"),
                ("Underdog Hero", "Wins when the odds were low.", "fa-arrow-up"),
                ("Iron Player", "Plays multiple matches in a night.", "fa-dumbbell"),
                ("The Anchor", "Always closes the night strong.", "fa-anchor"),
                ("Mr/Ms Consistent", "Steady performance week-to-week.", "fa-circle-check"),
                ("The Engineer", "Smart tactical positioning.", "fa-screwdriver-wrench"),
                ("Hustler of the Month", "Monthly top performer.", "fa-medal"),
                ("Season Veteran", "Played full season without missing.", "fa-ribbon"),
                ("Championship Mindset", "Always grinding for Vegas.", "fa-trophy"),
            ]
            for name, desc, icon in default_badges:
                conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO badges (name, description, icon) VALUES (?, ?, ?)",
                    (name, desc, icon),
                )
            # Explicitly remove deprecated badges
            conn.exec_driver_sql("DELETE FROM badges WHERE name = 'Top 4 Legend'")
            # Ensure playlist badge uses a supported icon
            conn.exec_driver_sql("UPDATE badges SET icon='fa-music' WHERE name='Playlist Curator'")
            # Set default roles
            conn.exec_driver_sql("UPDATE users SET role='member' WHERE role IS NULL OR role=''")
            # Ensure jross32 is admin
            conn.exec_driver_sql("UPDATE users SET role='admin' WHERE username='jross32'")
    except Exception as e:
        logging.warning(f"Locker migration check failed: {e}")
    session = SessionLocal()
    try:
        defaults = [
            ("general", "General Chat", "Everything team-wide goes here."),
            ("game-night", "Game Night", "Coordinating who's in, rides, and meetups."),
            ("strategies", "Strategies", "Matchups, orders, racks, and tactical notes."),
            ("opponent-intel", "Opponent Intel", "Scouting and strengths/weaknesses."),
            ("memes-media", "Memes & Media", "Clips, gifs, and lighthearted drops."),
            ("highlights", "Highlights", "Great shots and big moments."),
            ("announcements", "Announcements", "Captain/league notes."),
            ("requests", "Requests", "Gear, schedules, subs, and asks."),
            ("cuetillery", "Cüetillery", "Gear, cue tech, and arsenal talk."),
            ("profiles", "Player Profiles", "Future roster/spotlight area."),
        ]
        existing = {c.slug for c in session.query(Channel).all()}
        for idx, (slug, name, desc) in enumerate(defaults, start=1):
            if slug not in existing:
                session.add(Channel(slug=slug, name=name, description=desc, position=idx))
        session.commit()
    finally:
        session.close()


def locker_session():
    init_locker()
    return SessionLocal()


def initials_from_name(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()[:2]
    if parts:
        return parts[0][:2].upper()
    return "CL"


def list_league_players():
    data = load_league_data()
    players = data.get('players', {}) or {}
    out = []
    for pid, p in players.items():
        out.append({
            'id': pid,
            'name': p.get('full_name') or p.get('short_name') or pid
        })
    out.sort(key=lambda x: x['name'])
    return out


def list_cue_ligans_players():
    """Return only Cue-Ligans roster players (both 8 and 9) with names."""
    data = load_league_data()
    players = data.get('players', {}) or {}
    teams = data.get('teams', {}) or {}
    cue8, cue9 = find_cue_team_ids(data)
    roster_ids = set()
    for tid in [cue8, cue9]:
        t = teams.get(tid) or {}
        for entry in t.get('roster', []):
            pid = entry.get('player_id')
            if pid:
                roster_ids.add(pid)
    # Build an APA ID -> player key map to resolve numeric roster IDs
    apa_lookup = {}
    for key, pdata in players.items():
        apa_id = str(pdata.get('apa_id') or '').strip()
        if apa_id:
            apa_lookup[apa_id] = key
    out = []
    for pid in roster_ids:
        resolved_key = pid
        if pid not in players and pid in apa_lookup:
            resolved_key = apa_lookup[pid]
        p = players.get(resolved_key, {})
        display_name = p.get('full_name') or p.get('short_name') or p.get('apa_id') or resolved_key
        out.append({
            'id': resolved_key,
            'name': display_name
        })
    out.sort(key=lambda x: x['name'])
    return out

def list_all_players_for_select():
    """Return all players for linking profiles -> APA players."""
    data = load_league_data()
    players = data.get('players', {}) or {}
    out = []
    for key, p in players.items():
        name = p.get('full_name') or p.get('short_name') or key
        apa_id = p.get('apa_id')
        sls = p.get('current_skill_levels') or {}
        sl = sls.get('8-ball') or sls.get('9-ball') or p.get('sl') or p.get('skill_level')
        out.append({
            'id': key,
            'name': name,
            'apa_id': apa_id,
            'sl': sl
        })
    out.sort(key=lambda x: x['name'])
    return out

def resolve_player_for_user(user, players: dict):
    """Find a player dict for a user via player_ref or player_apa_id."""
    if not user or not players:
        return None
    # direct key
    if user.player_ref and user.player_ref in players:
        return players.get(user.player_ref)
    # apa id fallback
    if getattr(user, 'player_apa_id', None):
        for pdata in players.values():
            if str(pdata.get('apa_id') or '') == str(user.player_apa_id):
                return pdata
    # normalized name fallback
    if user.player_ref:
        target = normalize_team_name(user.player_ref)
        for pdata in players.values():
            name = pdata.get('full_name') or pdata.get('short_name') or ''
            if normalize_team_name(name) == target:
                return pdata
    return None


def get_current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    db = locker_session()
    try:
        return db.query(User).get(uid)
    except Exception:
        return None
    finally:
        db.close()

def unread_dm_count(user_id: int) -> int:
    if not user_id:
        return 0
    db = locker_session()
    try:
        return db.query(DirectMessage).filter(DirectMessage.recipient_id == user_id, DirectMessage.is_read == False).count()
    except Exception:
        return 0
    finally:
        db.close()

@app.context_processor
def inject_current_user():
    user = get_current_user()
    unread = unread_dm_count(user.id) if user else 0
    team_logo_url = get_team_logo_url(user.team_id if user else None)
    team_logo_alt = team_display_name(user.team_id) if user else 'Cue-ligans'
    brand_team_name = team_display_name(user.team_id) if user else 'Cue-ligans'
    brand_label = brand_team_name if user and user.team_id else 'CL'
    return {
        "current_user": user,
        "unread_dm_count": unread,
        "team_logo_url": team_logo_url,
        "team_logo_alt": team_logo_alt,
        "brand_team_name": brand_team_name,
        "brand_team_label": brand_label
    }


def is_admin(user):
    return bool(user and getattr(user, 'role', '') == 'admin')

def is_staff(user):
    role = (getattr(user, 'role', '') or '').lower()
    return role in ['admin', 'captain', 'coach', 'staff']

def get_user_by_username(username: str):
    if not username:
        return None
    db = locker_session()
    try:
        return db.query(User).filter(User.username == username).first()
    finally:
        db.close()


def log_action(actor_id, action, target='', detail=''):
    try:
        db = locker_session()
        entry = AuditLog(actor_user_id=actor_id, action=action, target=target, detail=detail, created_at=datetime.utcnow())
        db.add(entry)
        db.commit()
    except Exception as e:
        logging.error(f'Audit log failed: {e}')
    finally:
        try:
            db.close()
        except Exception:
            pass


# Badge helpers
def get_badge(session, name):
    return session.query(Badge).filter(Badge.name == name).first()


def user_has_badge(session, user_id, badge):
    if badge is None:
        return False
    return session.query(UserBadge).filter(UserBadge.user_id == user_id, UserBadge.badge_id == badge.id).first() is not None


def award_badge(session, user, badge_name, actor_id=None):
    """Award a badge to a user if not already present. Returns True if awarded."""
    badge = get_badge(session, badge_name)
    if not badge or user_has_badge(session, user.id, badge):
        return False
    session.add(UserBadge(user_id=user.id, badge_id=badge.id, awarded_at=datetime.utcnow()))
    session.commit()
    try:
        log_action(actor_id or user.id, 'award_badge', target=badge_name, detail=f'user:{user.username}')
    except Exception:
        pass
    return True


def award_badges_for_message(session, user, channel, message_obj):
    """Auto-award badges based on posting behavior."""
    # Totals
    total_msgs = session.query(func.count(Message.id)).filter(Message.user_id == user.id).scalar() or 0
    distinct_channels = session.query(Message.channel_id).filter(Message.user_id == user.id).distinct().count()
    # Channel buckets
    memes_channels = ['memes-media', 'memes', 'memes_media']
    strategy_channels = ['strategies', 'strategy', 'strats']
    general_channels = ['general', 'announcements']
    media_channels = ['memes-media', 'highlights', 'memes', 'memes_media']

    memes_count = session.query(func.count(Message.id)).join(Channel, Message.channel_id == Channel.id)\
        .filter(Message.user_id == user.id, Channel.slug.in_(memes_channels)).scalar() or 0
    strat_count = session.query(func.count(Message.id)).join(Channel, Message.channel_id == Channel.id)\
        .filter(Message.user_id == user.id, Channel.slug.in_(strategy_channels)).scalar() or 0
    general_count = session.query(func.count(Message.id)).join(Channel, Message.channel_id == Channel.id)\
        .filter(Message.user_id == user.id, Channel.slug.in_(general_channels)).scalar() or 0
    media_count = session.query(func.count(Message.id)).join(Channel, Message.channel_id == Channel.id)\
        .filter(Message.user_id == user.id, Channel.slug.in_(media_channels)).scalar() or 0

    # Time-of-day checks based on this message
    ts = message_obj.created_at or datetime.utcnow()
    hour = ts.hour

    # Auto-award rules
    award_badge(session, user, "Locker Rookie")
    if total_msgs >= 1:
        award_badge(session, user, "First Message")
    if memes_count >= 10:
        award_badge(session, user, "Memes Dealer")
    if strat_count >= 5:
        award_badge(session, user, "Shot Caller")
    if memes_count >= 15:
        award_badge(session, user, "Clown Shoes")
    if media_count >= 5:
        award_badge(session, user, "Media Dropper")
    if media_count >= 10:
        award_badge(session, user, "Uploader Pro")
    if general_count >= 15:
        award_badge(session, user, "Hype Master")
    if distinct_channels >= 4:
        award_badge(session, user, "Hyperactive")
    if distinct_channels >= 6:
        award_badge(session, user, "Channel Hopper")
    if total_msgs >= 100:
        award_badge(session, user, "Keyboard Warrior")
    if len(message_obj.content or '') >= 200:
        award_badge(session, user, "Deep Thinker")
    if len(message_obj.content or '') >= 400:
        award_badge(session, user, "Story Mode")
    if '😀' in (message_obj.content or '') or '😂' in (message_obj.content or ''):
        award_badge(session, user, "Emoji Addict")
    if total_msgs >= 30:
        award_badge(session, user, "Vibe Setter")
    if 0 <= hour < 5:
        award_badge(session, user, "Night Owl")
    if 5 <= hour < 9:
        award_badge(session, user, "Early Bird")
    # Manual / captaincy / skill badges are intentionally left for admin award.


def require_login(next_url=None):
    if not get_current_user():
        return redirect(url_for('login', next=next_url or request.url))
    return None


def require_admin():
    user = get_current_user()
    if not is_admin(user):
        flash('Admins only for that area.', 'warning')
        return redirect(url_for('dashboard'))
    return None


def serialize_user_for_view(user):
    if not user:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if get_current_user():
        return redirect(url_for('locker'))
    error = None
    team_options = build_team_options()
    default_team = team_options[0]['id'] if team_options else None
    selected_team_id = request.form.get('team_id') if request.method == 'POST' else default_team
    valid_team_ids = {opt['id'] for opt in team_options}
    if selected_team_id not in valid_team_ids:
        selected_team_id = default_team
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '')
        confirm = (request.form.get('confirm_password') or '')
        if len(username) < 3 or len(username) > 30:
            error = 'Username must be 3-30 characters.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            db = locker_session()
            try:
                if db.query(User).filter(User.username == username).first():
                    error = 'Username is already taken.'
                else:
                    user = User(username=username, display_name=username, initials=initials_from_name(username))
                    user.team_id = selected_team_id
                    user.set_password(password)
                    db.add(user)
                    db.commit()
                    session['user_id'] = user.id
                    session.permanent = True
                    return redirect(url_for('get_started'))
            finally:
                db.close()
    return render_template('signup.html', error=error, team_options=team_options, selected_team_id=selected_team_id)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_current_user():
        return redirect(url_for('locker'))
    error = None
    next_url = request.args.get('next') or request.form.get('next') or url_for('locker')
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        db = locker_session()
        try:
            user = db.query(User).filter(User.username == username).first()
            if not user or not user.check_password(password):
                error = 'Invalid username or password.'
            else:
                session['user_id'] = user.id
                remember = bool(request.form.get('remember_device'))
                session.permanent = remember
                return redirect(next_url)
        finally:
            db.close()
    return render_template('login.html', error=error, next=next_url)


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('locker'))


@app.route('/profiles')
def profiles():
    db = locker_session()
    users = []
    try:
        users = db.query(User).order_by(User.created_at.desc()).all()
    finally:
        db.close()
    return render_template('profiles.html', users=users)


@app.route('/profiles/<username>', methods=['GET', 'POST'])
def profile_detail(username):
    db = locker_session()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            abort(404)
        # load league data once for lookups (players, matches, etc.)
        league = load_league_data()
        players = league.get('players', {}) if isinstance(league, dict) else {}
        team_options = build_team_options(selected_id=user.team_id if user else None)
        # Handle nickname update for self
        me = get_current_user()
        if request.method == 'POST':
            if not (me and (me.id == user.id or is_staff(me))):
                if request.headers.get('X-Requested-With') == 'fetch-profile':
                    return jsonify({"ok": False, "error": "unauthorized"}), 403
                # fall back to normal render if no auth
                return redirect(url_for('profile_detail', username=user.username))
            # Allow admins/staff to link to any player; members restricted to Cue-ligans roster
            if is_staff(me):
                player_options = set(players.keys())
            else:
                player_options = {p['id'] for p in list_all_players_for_select()}
            players_map = players
            new_name = (request.form.get('display_name') or '').strip()
            new_bio = (request.form.get('bio') or '').strip()
            new_player = (request.form.get('player_ref') or '').strip()
            avatar_file = request.files.get('avatar_file')
            about_me = (request.form.get('about_me') or '').strip()
            interests = (request.form.get('interests') or '').strip()
            accent = (request.form.get('profile_accent_color') or '').strip()
            bg_url = (request.form.get('profile_background_url') or '').strip()
            banner_url = (request.form.get('profile_banner_url') or '').strip()
            bg_file = request.files.get('profile_background_file')
            banner_file = request.files.get('profile_banner_file')
            song_url = (request.form.get('theme_song_url') or '').strip()
            song_title = (request.form.get('theme_song_title') or '').strip()
            song_artist = (request.form.get('theme_song_artist') or '').strip()
            favorite_quote = (request.form.get('favorite_quote') or '').strip()
            favorite_game = (request.form.get('favorite_game') or '').strip()
            current_mood = (request.form.get('current_mood') or '').strip()
            linked_team = (request.form.get('linked_team') or '').strip()
            changed = False
            changed_fields = []
            update_data = {}
            logging.info("Profile save attempt for %s form=%s files=%s", user.username, dict(request.form), list(request.files.keys()))
            if 2 <= len(new_name) <= 40 and new_name != (user.display_name or user.username):
                user.add_nickname_history(user.display_name or user.username)
                update_data["display_name"] = new_name
                update_data["initials"] = initials_from_name(new_name)
                changed = True
                changed_fields.append("display_name")
            if new_bio != (user.bio or '') and len(new_bio) <= 280:
                update_data["bio"] = new_bio
                changed = True
                changed_fields.append("bio")
            if about_me != (user.about_me or ''):
                update_data["about_me"] = about_me
                changed = True
                changed_fields.append("about_me")
            if interests != (user.interests or ''):
                update_data["interests"] = interests
                changed = True
                changed_fields.append("interests")
            if accent != (user.profile_accent_color or ''):
                update_data["profile_accent_color"] = accent
                changed = True
                changed_fields.append("profile_accent_color")
            if bg_url and bg_url != (user.profile_background_url or ''):
                update_data["profile_background_url"] = bg_url
                changed = True
                changed_fields.append("profile_background_url")
            if banner_url and banner_url != (user.profile_banner_url or ''):
                update_data["profile_banner_url"] = banner_url
                changed = True
                changed_fields.append("profile_banner_url")
            if song_url != (user.theme_song_url or '') or song_title != (user.theme_song_title or '') or song_artist != (user.theme_song_artist or ''):
                update_data["theme_song_url"] = song_url
                update_data["theme_song_title"] = song_title
                update_data["theme_song_artist"] = song_artist
                changed = True
                changed_fields.append("theme_song")
            if favorite_quote != (user.favorite_quote or ''):
                update_data["favorite_quote"] = favorite_quote
                changed = True
                changed_fields.append("favorite_quote")
            if favorite_game != (user.favorite_game or ''):
                update_data["favorite_game"] = favorite_game
                changed = True
                changed_fields.append("favorite_game")
            if current_mood != (user.current_mood or ''):
                update_data["current_mood"] = current_mood
                changed = True
                changed_fields.append("current_mood")
            team_ids = {str(opt['id']) for opt in team_options}
            if linked_team and linked_team in team_ids and linked_team != (user.team_id or ''):
                update_data["team_id"] = linked_team
                changed = True
                changed_fields.append("team_id")
            if new_player in player_options and new_player != (user.player_ref or ''):
                # store both the internal player key and apa_id for lookups
                p_data = players_map.get(new_player, {})
                update_data["player_ref"] = new_player
                if p_data.get('apa_id'):
                    update_data["player_apa_id"] = str(p_data.get('apa_id'))
                changed = True
                changed_fields.append("player_ref")
            elif new_player and new_player not in player_options:
                if request.headers.get('X-Requested-With') == 'fetch-profile':
                    return jsonify({"ok": False, "error": "invalid_player", "detail": "Selected player not on Cue-Ligans roster"}), 400
                flash('Selected player is not on the Cue-Ligans roster.', 'danger')
                return redirect(url_for('profile_detail', username=user.username))
            # Handle avatar uploads (supports fetch JSON response)
            if avatar_file and avatar_file.filename:
                filename = secure_filename(avatar_file.filename)
                ext = filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg','gif','webp','bmp','svg','jfif','pjpeg','pjp']:
                    upload_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    unique_name = f"user_{user.id}_{int(datetime.utcnow().timestamp())}.{ext}"
                    path = os.path.join(upload_dir, unique_name)
                    avatar_file.save(path)
                    update_data["avatar_url"] = f"/static/uploads/{unique_name}"
                    changed = True
                    changed_fields.append("avatar_url")
            # Handle background upload
            if bg_file and bg_file.filename:
                filename = secure_filename(bg_file.filename)
                ext = filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg','gif','webp','bmp','svg','jfif','pjpeg','pjp']:
                    upload_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    unique_name = f"user_{user.id}_bg_{int(datetime.utcnow().timestamp())}.{ext}"
                    path = os.path.join(upload_dir, unique_name)
                    bg_file.save(path)
                    update_data["profile_background_url"] = f"/static/uploads/{unique_name}"
                    changed = True
                    changed_fields.append("profile_background_url")
            # Handle banner upload
            if banner_file and banner_file.filename:
                filename = secure_filename(banner_file.filename)
                ext = filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg','gif','webp','bmp','svg','jfif','pjpeg','pjp']:
                    upload_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    unique_name = f"user_{user.id}_banner_{int(datetime.utcnow().timestamp())}.{ext}"
                    path = os.path.join(upload_dir, unique_name)
                    banner_file.save(path)
                    update_data["profile_banner_url"] = f"/static/uploads/{unique_name}"
                    changed = True
                    changed_fields.append("profile_banner_url")
            # If nothing changed, respond clearly
            if not changed:
                if request.headers.get('X-Requested-With') == 'fetch-profile':
                    return jsonify({"ok": False, "error": "no_changes", "detail": "No fields were modified"}), 200
                flash('No changes to save.', 'warning')
                return redirect(url_for('profile_detail', username=user.username))
            if changed:
                try:
                    # Use UPDATE to guarantee persistence, then refresh
                    db.query(User).filter(User.id == user.id).update(update_data, synchronize_session=False)
                    db.commit()
                    user = db.query(User).get(user.id)
                    fresh_name = user.display_name if user else None
                    logging.info("Profile saved for %s fields=%s now display_name=%s db_path=%s update_data=%s", (user.username if user else 'unknown'), changed_fields, fresh_name, LOCKER_DB, update_data)
                except Exception as e:
                    db.rollback()
                    if request.headers.get('X-Requested-With') == 'fetch-profile':
                        return jsonify({"ok": False, "error": "save_failed", "detail": str(e)})
                    raise
                try:
                    if accent or bg_url:
                        award_badge(db, user, "Theme Lord", actor_id=user.id)
                        award_badge(db, user, "Glow Architect", actor_id=user.id)
                    if bg_url:
                        award_badge(db, user, "Wallpaper Wizard", actor_id=user.id)
                    if song_url:
                        award_badge(db, user, "Audiophile", actor_id=user.id)
                        award_badge(db, user, "Playlist Curator", actor_id=user.id)
                    if new_bio and len(new_bio) >= 120:
                        award_badge(db, user, "Bio Poet", actor_id=user.id)
                    if current_mood:
                        award_badge(db, user, "Mood Setter", actor_id=user.id)
                    if new_name != (user.display_name or user.username):
                        award_badge(db, user, "Design Freak", actor_id=user.id)
                except Exception as e:
                    logging.warning(f'Profile badge award failed: {e}')
            else:
                # ensure we still return a consistent payload
                db.refresh(user)
            # Reload fresh values to verify persistence
            try:
                updated = db.query(User).get(user.id)
            except Exception:
                updated = user
            # If avatar was uploaded via fetch, return JSON to avoid full reload
            if avatar_file and request.headers.get('X-Requested-With') == 'fetch-avatar':
                if user and user.avatar_url:
                    return jsonify({"ok": True, "avatar_url": f"{user.avatar_url}?v={int(datetime.utcnow().timestamp())}", "changed_fields": changed_fields})
                return jsonify({"ok": False, "error": "avatar_save_failed"})
            # Profile detail save via modal (AJAX)
            if request.headers.get('X-Requested-With') == 'fetch-profile':
                if not updated:
                    return jsonify({"ok": False, "error": "verify_failed", "detail": "User reload failed"})
                return jsonify({
                    "ok": True,
                    "changed": changed,
                    "message": "Profile updated" if changed else "No changes detected",
                    "changed_fields": changed_fields,
                    "debug": {
                        "form": dict(request.form),
                        "player_options": list(player_options),
                        "user_id": user.id,
                        "updated_values": {
                            "display_name": updated.display_name,
                            "bio": updated.bio,
                            "about_me": updated.about_me,
                            "interests": updated.interests,
                            "profile_accent_color": updated.profile_accent_color,
                            "profile_background_url": updated.profile_background_url,
                            "theme_song_url": updated.theme_song_url,
                            "theme_song_title": updated.theme_song_title,
                            "theme_song_artist": updated.theme_song_artist,
                            "favorite_quote": updated.favorite_quote,
                            "favorite_game": updated.favorite_game,
                            "current_mood": updated.current_mood,
                            "player_ref": updated.player_ref,
                            "team_id": updated.team_id,
                        }
                    },
                    "profile": {
                        "display_name": updated.display_name or updated.username,
                        "username": updated.username,
                        "bio": updated.bio or "",
                        "about_me": updated.about_me or "",
                        "interests": updated.interests or "",
                        "theme_song_url": updated.theme_song_url or "",
                        "theme_song_title": updated.theme_song_title or "",
                        "theme_song_artist": updated.theme_song_artist or "",
                        "favorite_quote": updated.favorite_quote or "",
                        "favorite_game": updated.favorite_game or "",
                        "current_mood": updated.current_mood or "",
                        "profile_accent_color": updated.profile_accent_color or "",
                        "profile_background_url": updated.profile_background_url or "",
                        "profile_banner_url": updated.profile_banner_url or "",
                        "player_ref": updated.player_ref or "",
                        "team_id": updated.team_id or ""
                    }
                })
            flash('Profile updated', 'success')
            return redirect(url_for('profile_detail', username=user.username))
        # quick stats
        message_count = db.query(Message).filter(Message.user_id == user.id).count()
        channel_count = db.query(Message.channel_id).filter(Message.user_id == user.id).distinct().count()
        last_message = (
            db.query(Message)
            .filter(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        comments = (
            db.query(ProfileComment)
            .filter(ProfileComment.profile_user_id == user.id)
            .order_by(ProfileComment.created_at.desc())
            .limit(20)
            .all()
        )
        commenter_ids = {c.author_user_id for c in comments}
        commenters = {u.id: u for u in db.query(User).filter(User.id.in_(commenter_ids)).all()} if commenter_ids else {}
        recent_messages = (
            db.query(Message)
            .filter(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(10)
            .all()
        )
        channel_lookup = {}
        for m in recent_messages:
            if m.channel:
                channel_lookup[m.channel_id] = m.channel
        other_users = (
            db.query(User)
            .filter(User.id != user.id)
            .order_by(User.created_at.desc())
            .limit(3)
            .all()
        )
        player_options = list_all_players_for_select()
        # Match history for the linked player
        match_history = {'8-ball': [], '9-ball': []}
        seen_matches = set()
        chosen_player_id = user.player_ref
        apa_profile_url = None
        if not chosen_player_id:
            def norm(val):
                return ''.join(ch.lower() for ch in (val or '') if ch.isalnum())
            uname = norm(user.username)
            dname = norm(user.display_name or user.username)
            players_map = league.get('players', {}) or {}
            candidates = []
            for pid, pdata in players_map.items():
                pname = norm(pdata.get('full_name') or pdata.get('short_name') or '')
                if pname and (uname in pname or pname in uname or dname in pname or pname in dname):
                    candidates.append(pid)
            if len(candidates) == 1:
                chosen_player_id = candidates[0]
        if chosen_player_id:
            try:
                # Build date->week map
                date_to_week = {}
                schedule_json = load_json('schedule.json') or []
                for wk in schedule_json:
                    if isinstance(wk, dict) and wk.get('date'):
                        date_to_week[wk['date']] = wk.get('week', 0)
                player_meta = players.get(chosen_player_id) or {}
                apa_profile_url = (player_meta.get('urls') or {}).get('profile') or player_meta.get('profile_url')
                matches = league.get('matches', {}) or {}
                for m in matches.values():
                    fmt = m.get('format', '')
                    if fmt not in ('8-ball', '9-ball'):
                        continue
                    date = m.get('date')
                    week_num = date_to_week.get(date)
                    sets = m.get('sets', []) or []
                    for s in sets:
                        if not isinstance(s, dict):
                            continue
                        our_sl = opp_sl = None
                        our_pts = opp_pts = None
                        opponent_id = None
                        if s.get('home_player_id') == chosen_player_id:
                            our_sl = s.get('home_skill_level')
                            opp_sl = s.get('away_skill_level')
                            our_pts = s.get('home_points', s.get('home_score_raw'))
                            opp_pts = s.get('away_points', s.get('away_score_raw'))
                            opponent_id = s.get('away_player_id')
                        elif s.get('away_player_id') == chosen_player_id:
                            our_sl = s.get('away_skill_level')
                            opp_sl = s.get('home_skill_level')
                            our_pts = s.get('away_points', s.get('away_score_raw'))
                            opp_pts = s.get('home_points', s.get('home_score_raw'))
                            opponent_id = s.get('home_player_id')
                        else:
                            continue
                        opp_name = players.get(opponent_id, {}).get('full_name') or opponent_id
                        try:
                            d_parsed = datetime.fromisoformat(date) if date else None
                        except Exception:
                            d_parsed = None
                        key = (fmt, date, opponent_id, our_pts, opp_pts, our_sl, opp_sl, week_num)
                        if key in seen_matches:
                            continue
                        seen_matches.add(key)
                        match_history[fmt].append({
                            'date': date,
                            'week': week_num,
                            'opponent': opp_name,
                            'opponent_sl': opp_sl,
                            'our_sl': our_sl,
                            'our_pts': our_pts,
                            'opp_pts': opp_pts,
                            'result': 'Win' if (our_pts is not None and opp_pts is not None and our_pts > opp_pts) else 'Loss' if (our_pts is not None and opp_pts is not None and our_pts < opp_pts) else '-',
                            'dt': d_parsed,
                        })
                for fmt in match_history:
                    match_history[fmt].sort(key=lambda x: (x['dt'] or datetime.min), reverse=True)
            except Exception as e:
                logging.warning(f'Could not build match history for {user.player_ref}: {e}')
        # Patch index + derived counts
        patch_index = load_patch_index() or {"categories": [], "difficulty_scale": {}}
        patch_lookup = {}
        for cat in patch_index.get('categories', []):
            for p in cat.get('patches', []):
                if p.get('code'):
                    patch_lookup[p['code']] = p
        patch_counts = compute_patch_counts(chosen_player_id, league) if chosen_player_id else {}
        diff_rank = {
            'mythic': 6,
            'diamond': 5,
            'platinum': 4,
            'gold': 3,
            'silver': 2,
            'bronze': 1
        }
        def rank_of(diff):
            return diff_rank.get(str(diff or '').lower(), 0)
        earned = []
        for code, cnt in (patch_counts or {}).items():
            info = patch_lookup.get(code, {"name": code, "description": "", "difficulty": ""})
            earned.append({
                "code": code,
                "name": info.get("name", code),
                "description": info.get("description", ""),
                "difficulty": info.get("difficulty", ""),
                "count": cnt,
                "locked": False
            })
        earned = sorted(earned, key=lambda x: (-rank_of(x.get("difficulty")), -x.get("count", 0), x.get("name", "")))
        showcase = list(earned)
        if len(showcase) < 4:
            for cat in patch_index.get('categories', []):
                for p in cat.get('patches', []):
                    if p.get('code') in patch_counts:
                        continue
                    showcase.append({
                        "code": p.get("code"),
                        "name": p.get("name"),
                        "description": p.get("description", ""),
                        "difficulty": p.get("difficulty", ""),
                        "count": 0,
                        "locked": True
                    })
                    if len(showcase) >= 4:
                        break
                if len(showcase) >= 4:
                    break
        # Re-sort to keep difficulty hierarchy for display
        showcase = sorted(showcase, key=lambda x: (-rank_of(x.get("difficulty")), -x.get("count", 0), x.get("name", "")))
        patch_showcase = showcase[:4]
        # User badges lookup
        badge_rows = (
            db.query(UserBadge)
            .filter(UserBadge.user_id == user.id)
            .order_by(UserBadge.awarded_at.desc())
            .all()
        )
        user_badges = {b.badge.name for b in badge_rows if b.badge}
        all_badges = db.query(Badge).all()
        unlocked_badges = [b.badge for b in badge_rows if b.badge]
        locked_badges = [b for b in all_badges if b.name not in user_badges]
        ordered_badges = unlocked_badges + locked_badges

        # Mini "ESPN" style insights for the selected player (best-effort from league data)
        player_insights = None
        try:
            league = load_league_data()
            players_map = league.get('players', {}) or {}
            p = resolve_player_for_user(user, players_map)
            if not p:
                raise ValueError("Player not resolved for profile")

            def fmt_num(val, decimals=2, pct=False, default='-'):
                if val in (None, '', '-', 'None'):
                    return default
                try:
                    v = float(val)
                except Exception:
                    return default
                if pct:
                    v *= 100
                return f"{v:.{decimals}f}" + ("%" if pct else "")

            def fmt_pct(val, decimals=2, default='-'):
                frac = normalize_percent(val)
                if frac is None:
                    return default
                return f"{frac * 100:.{decimals}f}%"

            def fmt_int(val):
                try:
                    return int(val)
                except Exception:
                    return None

            def fmt_date(val):
                if not val:
                    return '-'
                try:
                    iso = str(val).replace('Z', '+00:00')
                    dt = datetime.fromisoformat(iso)
                    return dt.strftime('%B %d, %Y')
                except Exception:
                    return '-'

            sessions = p.get('sessions') or {}
            # Ensure we always have placeholders for both formats to render cards
            if isinstance(sessions, dict):
                sessions.setdefault('8-ball', {})
                sessions.setdefault('9-ball', {})
            # normalize sessions if they arrived as a list
            if isinstance(sessions, list):
                norm = {}
                for sess in sessions:
                    fmt = (sess or {}).get('format')
                    if fmt:
                        norm[fmt] = sess
                # ensure keys exist for both formats to render cards
                sessions = norm
                for missing_fmt in ('8-ball', '9-ball'):
                    sessions.setdefault(missing_fmt, {})

            sls = p.get('current_skill_levels', {}) or {}
            fmt_keys = {
                '8-ball': ['8-ball', '8_ball', '8', 'eight_ball'],
                '9-ball': ['9-ball', '9_ball', '9', 'nine_ball']
            }
            fmt_stats = {}
            for fmt_label, keys in fmt_keys.items():
                sess = None
                for k in keys:
                    if k in sessions:
                        sess = sessions.get(k) or {}
                        break
                if not sess:
                    # Fallback to new schema stats block (including 'unknown')
                    stats_block = p.get('stats') or {}
                    if fmt_label in stats_block:
                        sess = stats_block.get(fmt_label) or {}
                    elif 'unknown' in stats_block:
                        sess = stats_block.get('unknown') or {}
                    elif len(stats_block) == 1:
                        sess = list(stats_block.values())[0] or {}
                # derive trend from recent match history
                recent = match_history.get(fmt_label, []) if match_history else []
                recent5 = [m for m in recent if m.get('result') in ('Win', 'Loss')][:5]
                trend_label = None
                base_win = (sess or {}).get('win_pct') or (sess or {}).get('pa')
                base_ppm = (sess or {}).get('points_per_match') or (sess or {}).get('ppm')
                if recent5:
                    wins = sum(1 for m in recent5 if m.get('result') == 'Win')
                    rec_win_pct = wins / len(recent5)
                    rec_ppm = sum((m.get('our_pts') or 0) for m in recent5) / len(recent5)
                    # choose ppm if available, else win%
                    metric_delta = None
                    if base_ppm is not None:
                        metric_delta = rec_ppm - base_ppm
                    elif base_win is not None:
                        metric_delta = rec_win_pct - base_win
                    if metric_delta is not None:
                        if metric_delta > 0.5:
                            trend_label = "Up"
                        elif metric_delta < -0.5:
                            trend_label = "Down"
                        else:
                            trend_label = "Stable"
                # If we have stats but no recent matches, mark as stable instead of n/a
                if trend_label is None and (base_ppm is not None or base_win is not None):
                    trend_label = "Stable"
                # Skill level fallback from stats->skill_level
                sl_value = next((sls.get(k) for k in keys if k in sls), None)
                if not sl_value:
                    sl_value = (sess or {}).get('skill_level') or (sess or {}).get('sl')
                played = fmt_int((sess or {}).get('matches_played'))
                wins = fmt_int((sess or {}).get('matches_won'))
                losses = fmt_int((sess or {}).get('matches_lost'))
                if losses is None and played is not None and wins is not None:
                    losses = played - wins if played >= wins else None
                fmt_stats[fmt_label] = {
                    'session_label': (sess or {}).get('session') or (sess or {}).get('session_name') or '-',
                    'sl': sl_value,
                    'win_pct': fmt_num((sess or {}).get('win_pct'), pct=True),
                    'ppm': fmt_num((sess or {}).get('points_per_match')),
                    'pa': fmt_num((sess or {}).get('percent_points_avail'), pct=True),
                    'matches_played': played if played is not None else '-',
                    'wins': wins if wins is not None else '-',
                    'losses': losses if losses is not None else '-',
                    'trend': trend_label or (sess or {}).get('trend') or '-'
                }
            # Ensure both formats are present so UI renders both cards even if missing data
            for fmt_label in ('8-ball', '9-ball'):
                if fmt_label not in fmt_stats:
                    fmt_stats[fmt_label] = {
                        'sl': None,
                        'win_pct': '-',
                        'ppm': '-',
                        'pa': '-',
                        'matches_played': '-',
                        'wins': '-',
                        'losses': '-',
                        'trend': 'No data'
                    }

            lifetime_stats = p.get('lifetime_stats') or {}
            lifetime_ext = p.get('lifetime_extended') or {}
            lifetime_formats = []
            lifetime_ext_formatted = {}
            for fmt_label in ('8-ball', '9-ball'):
                raw = lifetime_stats.get(fmt_label) or lifetime_stats.get(fmt_label.replace('-', '_')) or {}
                ext = lifetime_ext.get(fmt_label) or lifetime_ext.get(fmt_label.replace('-', '_')) or {}
                lp = fmt_int(raw.get('matchesPlayed') or raw.get('matches') or raw.get('games'))
                lw = fmt_int(raw.get('matchesWon') or raw.get('wins') or raw.get('won'))
                ll = fmt_int(raw.get('losses') or raw.get('lost'))
                if lp is None and ext.get('matchesPlayed') is not None:
                    lp = fmt_int(ext.get('matchesPlayed'))
                if lw is None and ext.get('matchesWon') is not None:
                    lw = fmt_int(ext.get('matchesWon'))
                if ll is None and ext.get('losses') is not None:
                    ll = fmt_int(ext.get('losses'))
                if ll is None and lp is not None and lw is not None:
                    ll = lp - lw if lp >= lw else None
                win_pct_raw = normalize_percent(raw.get('win_pct') or raw.get('pa'))
                if win_pct_raw is None and lp and lw is not None and lp > 0:
                    win_pct_raw = lw / lp
                win_pct = fmt_pct(win_pct_raw)
                ppm = fmt_num(raw.get('points_per_match') or raw.get('ppm') or raw.get('pointsPerMatch'))
                pa_raw = normalize_percent(raw.get('percent_points_avail') or raw.get('pa') or win_pct_raw)
                pa = fmt_pct(pa_raw)
                cla = fmt_num(raw.get('CLA') if raw.get('CLA') not in (None, '-') else ext.get('CLA'), decimals=1)
                def_avg = fmt_num(raw.get('defensiveShotAvg') if raw.get('defensiveShotAvg') not in (None, '-') else ext.get('defensiveShotAvg'))
                last_played = fmt_date(ext.get('lastPlayed'))
                last_two = ext.get('matchCountForLastTwoYrs') or '-'
                has_data = any([
                    lp not in (None, '-'),
                    lw not in (None, '-'),
                    win_pct_raw not in (None, '-'),
                    ppm not in (None, '-'),
                    pa_raw not in (None, '-'),
                    cla not in (None, '-'),
                    def_avg not in (None, '-')
                ])
                card = {
                    'label': fmt_label,
                    'win_pct': win_pct,
                    'ppm': ppm,
                    'pa': pa,
                    'matches': lp if lp is not None else '-',
                    'wins': lw if lw is not None else '-',
                    'losses': ll if ll is not None else '-',
                    'sl': raw.get('sl') or raw.get('skill_level') or sls.get(fmt_label) or sls.get(fmt_label.replace('_','-')) or '-',
                    'cla': cla,
                    'def_avg': def_avg,
                    'last_played': last_played,
                    'last_two_years': last_two,
                    'has_data': has_data
                }
                lifetime_formats.append(card)
                lifetime_ext_formatted[fmt_label] = {
                    'matchesPlayed': card['matches'],
                    'matchesWon': card['wins'],
                    'losses': card['losses'],
                    'CLA': card['cla'],
                    'defensiveShotAvg': card['def_avg'],
                    'matchCountForLastTwoYrs': card['last_two_years'],
                    'lastPlayed': card['last_played'],
                }

            lifetime_summary = lifetime_formats[0] if lifetime_formats else None

            raw_highlights = p.get('session_highlights') or []
            # Build grouped season summaries with friendly labels and filtered stats
            stat_labels = {
                'eight_ball_break_and_runs': 'Break & Run (8-Ball)',
                'nine_ball_break_and_runs': 'Break & Run (9-Ball)',
                'eight_on_breaks': '8-On-The-Break',
                'nine_on_snaps': '9-On-The-Snap',
                'rackless': 'Rackless Nights',
                'skunks': 'Skunks'
            }

            def season_year(name):
                if not name:
                    return 0
                parts = str(name).split()
                try:
                    return int(parts[-1])
                except Exception:
                    return 0

            past_sessions_grouped = []
            by_season = {}
            for h in raw_highlights:
                if not isinstance(h, dict):
                    continue
                sname = h.get('session_name') or f"Session {h.get('session_id') or '-'}"
                fmt = (h.get('format') or '').lower()
                fmt_label = '8-ball' if '8' in fmt else '9-ball'
                year = season_year(sname)
                season_key = (year, sname)
                block = by_season.setdefault(season_key, {
                    'season_name': sname,
                    'year': year,
                    'formats': {},
                    'session_id': h.get('session_id') or '',
                    'session_key': h.get('session_id') or sname
                })
                if not block.get('session_id') and h.get('session_id'):
                    block['session_id'] = h.get('session_id')
                fmt_bucket = block['formats'].setdefault(fmt_label, {})
                for key, label in stat_labels.items():
                    val = h.get(key)
                    if val is None:
                        continue
                    try:
                        count = int(val)
                    except Exception:
                        count = 1
                    if count <= 0:
                        continue
                    fmt_bucket[label] = fmt_bucket.get(label, 0) + count
            # Convert aggregated map to list structure and drop empties
            for _, block in by_season.items():
                block['session_key'] = block.get('session_key') or block['season_name']
                fmt_list = []
                for fmt_label, stats_map in block['formats'].items():
                    stats = [{'label': lbl, 'count': val} for lbl, val in stats_map.items() if val]
                    if stats:
                        fmt_list.append({'format': fmt_label, 'stats': stats})
                fmt_list.sort(key=lambda x: 0 if x['format'].startswith('8') else 1)
                block['formats'] = fmt_list
                past_sessions_grouped.append(block)
            past_sessions_grouped.sort(key=lambda x: (x.get('year') or 0, x.get('season_name') or ''), reverse=True)
            session_filters = []
            for block in past_sessions_grouped:
                session_filters.append({
                    'session_name': block['season_name'],
                    'session_id': block.get('session_id'),
                    'session_key': block.get('session_key'),
                    'year': block.get('year')
                })
            current_session = session_filters[0]['session_name'] if session_filters else None
            active_session_key = session_filters[0]['session_key'] if session_filters else None
            highlight_labels = sorted(
                {
                    stat['label']
                    for block in past_sessions_grouped
                    for fmt in block['formats']
                    for stat in fmt['stats']
                }
            )

            membership_years = p.get('membership_years') or []
            if isinstance(membership_years, list):
                membership_years = sorted({y for y in membership_years if isinstance(y, int)}, reverse=True)

            membership_leagues_raw = p.get('membership_leagues') or []
            seen_membership = set()
            membership_leagues = []
            for league_entry in membership_leagues_raw:
                lid = league_entry.get('league_id')
                name = league_entry.get('league_name')
                key = (lid, name)
                if key in seen_membership or not name:
                    continue
                seen_membership.add(key)
                membership_leagues.append(league_entry)

            player_insights = {
                'name': p.get('full_name') or p.get('short_name') or user.display_name or user.username,
                'head_to_head': 'Head-to-head data coming soon.',
                'lifetime': p.get('lifetime_stats') or {},
                'lifetime_summary': lifetime_summary,
                'lifetime_formats': lifetime_formats,
                'past_sessions_grouped': past_sessions_grouped,
                'session_filters': session_filters,
                'active_session_key': active_session_key,
                'current_session': current_session,
                'highlight_labels': highlight_labels,
                'formats': fmt_stats,
                'membership_years': membership_years,
                'session_highlights': p.get('session_highlights') or [],
                'lifetime_extended': lifetime_ext_formatted,
                'membership_leagues': membership_leagues,
                'consecutive_years_played': p.get('consecutive_years_played')
            }
        except Exception as e:
            logging.warning(f"player_insights build failed for {user.player_ref}: {e}")

        return render_template(
            'profile_detail.html',
            user=user,
            recent_messages=recent_messages,
            message_count=message_count,
            channel_count=channel_count,
            last_message=last_message,
            channel_lookup=channel_lookup,
            now=datetime.utcnow(),
            player_options=player_options,
            team_options=team_options,
            comments=comments,
            commenters=commenters,
            user_badges=user_badges,
            all_badges=all_badges,
            badges_ordered=ordered_badges,
            other_users=other_users,
            player_insights=player_insights,
            match_history=match_history,
            patch_index=patch_index,
            patch_counts=patch_counts,
            patch_showcase=patch_showcase,
            apa_profile_url=apa_profile_url
        )
    finally:
        db.close()


@app.route('/profiles/<username>/comment', methods=['POST'])
def profile_comment(username):
    user = get_current_user()
    if not user:
        return redirect(url_for('login', next=url_for('profile_detail', username=username)))
    if user.is_banned or user.is_suspended or user.is_muted:
        flash('Commenting is disabled for your account.', 'danger')
        return redirect(url_for('profile_detail', username=username))
    db = locker_session()
    try:
        target = db.query(User).filter(User.username == username).first()
        if not target:
            abort(404)
        content = (request.form.get('content') or '').strip()
        if content:
            comment = ProfileComment(profile_user_id=target.id, author_user_id=user.id, content=content, created_at=datetime.utcnow())
            db.add(comment)
            db.commit()
        return redirect(url_for('profile_detail', username=username))
    finally:
        db.close()

@app.route('/messages/send', methods=['POST'])
def send_direct_message():
    sender = get_current_user()
    if not sender:
        return redirect(url_for('login', next=request.referrer or url_for('messenger')))
    recipient_username = (request.form.get('recipient') or '').strip()
    content = (request.form.get('content') or '').strip()
    if not recipient_username or not content:
        flash('Message cannot be empty.', 'warning')
        return redirect(request.referrer or url_for('messenger'))
    db = locker_session()
    try:
        recipient = db.query(User).filter(User.username == recipient_username).first()
        if not recipient:
            flash('Recipient not found.', 'danger')
            return redirect(request.referrer or url_for('messenger'))
        msg = DirectMessage(sender_id=sender.id, recipient_id=recipient.id, content=content, created_at=datetime.utcnow())
        db.add(msg)
        db.commit()
        flash('Message sent.', 'success')
        return redirect(url_for('messenger', user=recipient.id))
    finally:
        db.close()

@app.route('/messenger')
def messenger():
    user = get_current_user()
    if not user:
        return redirect(url_for('login', next=url_for('messenger')))
    db = locker_session()
    try:
        target_id = request.args.get('user')
        try:
            target_id = int(target_id) if target_id else None
        except Exception:
            target_id = None
        # conversation list
        threads = []
        seen_ids = set()
        q = db.query(DirectMessage).filter(
            (DirectMessage.sender_id == user.id) | (DirectMessage.recipient_id == user.id)
        ).order_by(DirectMessage.created_at.desc()).all()
        for m in q:
            other_id = m.sender_id if m.sender_id != user.id else m.recipient_id
            if other_id in seen_ids:
                continue
            seen_ids.add(other_id)
            other = db.query(User).get(other_id)
            if not other:
                continue
            unread = db.query(DirectMessage).filter(DirectMessage.recipient_id == user.id,
                                                    DirectMessage.sender_id == other_id,
                                                    DirectMessage.is_read == False).count()
            threads.append({
                "user": serialize_user_for_view(other),
                # Detach-safe payload
                "last": {
                    "content": m.content,
                    "created_at": m.created_at,
                    "sender_id": m.sender_id,
                    "recipient_id": m.recipient_id,
                },
                "unread": unread
            })
        if target_id is None and threads:
            target_id = threads[0]['user']['id']
        messages = []
        target_user_obj = db.query(User).get(target_id) if target_id else None
        if target_user_obj:
            messages = db.query(DirectMessage).filter(
                ((DirectMessage.sender_id == user.id) & (DirectMessage.recipient_id == target_user_obj.id)) |
                ((DirectMessage.sender_id == target_user_obj.id) & (DirectMessage.recipient_id == user.id))
            ).order_by(DirectMessage.created_at.asc()).all()
            # mark as read
            db.query(DirectMessage).filter(
                DirectMessage.recipient_id == user.id,
                DirectMessage.sender_id == target_user_obj.id,
                DirectMessage.is_read == False
            ).update({"is_read": True})
            db.commit()
        # Build sender name cache to avoid lazy loads
        id_set = set()
        for m in messages:
            id_set.add(m.sender_id)
            id_set.add(m.recipient_id)
        if target_user_obj:
            id_set.add(target_user_obj.id)
        id_set.discard(None)
        user_cache = {}
        if id_set:
            for uobj in db.query(User).filter(User.id.in_(list(id_set))).all():
                user_cache[uobj.id] = uobj
        def name_for(uid):
            uobj = user_cache.get(uid)
            if not uobj:
                return f"User {uid}"
            return uobj.display_name or uobj.username
        messages_view = [{
            "content": m.content,
            "created_at": m.created_at,
            "sender_id": m.sender_id,
            "recipient_id": m.recipient_id,
            "sender_name": name_for(m.sender_id),
            "from_me": m.sender_id == user.id,
        } for m in messages]
        target_user = serialize_user_for_view(target_user_obj)
        return render_template('messenger.html', threads=threads, messages=messages_view, target=target_user)
    finally:
        db.close()


@app.route('/admin/users/role', methods=['POST'])
def admin_set_role():
    guard = require_admin()
    if guard:
        return guard
    target_id_raw = request.form.get('user_id')
    try:
        target_id = int(target_id_raw)
    except (TypeError, ValueError):
        target_id = None
    new_role = request.form.get('role', '').strip()
    db = locker_session()
    try:
        target = db.query(User).get(target_id) if target_id else None
        if target and new_role:
            target.role = new_role
            db.commit()
            flash(f'Updated role for {target.username} to {new_role}', 'success')
            log_action(get_current_user().id, 'set_role', target=target.username, detail=new_role)
        else:
            flash('Unable to update role (invalid user or role).', 'danger')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/messages/delete', methods=['POST'])
def admin_delete_message():
    guard = require_admin()
    if guard:
        return guard
    mid = request.form.get('message_id')
    db = locker_session()
    try:
        msg = db.query(Message).get(mid)
        if msg:
            db.delete(msg)
            db.commit()
            flash('Message deleted.', 'info')
            log_action(get_current_user().id, 'delete_message', target=str(mid))
    finally:
        db.close()
    return redirect(request.referrer or url_for('locker'))


@app.route('/admin/comments/delete', methods=['POST'])
def admin_delete_comment():
    guard = require_admin()
    if guard:
        return guard
    cid = request.form.get('comment_id')
    db = locker_session()
    try:
        c = db.query(ProfileComment).get(cid)
        if c:
            db.delete(c)
            db.commit()
            flash('Comment deleted.', 'info')
            log_action(get_current_user().id, 'delete_comment', target=str(cid))
    finally:
        db.close()
    return redirect(request.referrer or url_for('locker'))


@app.route('/admin/users/suspend', methods=['POST'])
def admin_suspend_user():
    guard = require_admin()
    if guard:
        return guard
    target_id = request.form.get('user_id')
    action = request.form.get('action', 'suspend')
    db = locker_session()
    try:
        target = db.query(User).get(target_id)
        if target:
            if action == 'suspend':
                target.is_suspended = True
                msg = f'Suspended {target.username}'
                log_action(get_current_user().id, 'suspend_user', target=target.username)
            else:
                target.is_suspended = False
                msg = f'Reactivated {target.username}'
                log_action(get_current_user().id, 'reactivate_user', target=target.username)
            db.commit()
            flash(msg, 'info')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/users/reset_password', methods=['POST'])
def admin_reset_password():
    guard = require_admin()
    if guard:
        return guard
    target_id = request.form.get('user_id')
    temp_password = request.form.get('temp_password', 'changeme123')
    db = locker_session()
    try:
        target = db.query(User).get(target_id)
        if target:
            target.set_password(temp_password)
            db.commit()
            log_action(get_current_user().id, 'reset_password', target=target.username)
            flash(f"Password reset for {target.username}. Temp password: {temp_password}", 'warning')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/users/mute', methods=['POST'])
def admin_mute_user():
    guard = require_admin()
    if guard:
        return guard
    target_id = request.form.get('user_id')
    action = request.form.get('action', 'mute')
    db = locker_session()
    try:
        target = db.query(User).get(target_id)
        if target:
            target.is_muted = (action == 'mute')
            db.commit()
            log_action(get_current_user().id, f"{'mute' if action=='mute' else 'unmute'}_user", target=target.username)
            flash(f"{'Muted' if action=='mute' else 'Unmuted'} {target.username}", 'info')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/users/ban', methods=['POST'])
def admin_ban_user():
    guard = require_admin()
    if guard:
        return guard
    target_id = request.form.get('user_id')
    action = request.form.get('action', 'ban')
    db = locker_session()
    try:
        target = db.query(User).get(target_id)
        if target:
            target.is_banned = (action == 'ban')
            db.commit()
            log_action(get_current_user().id, f"{'ban' if action=='ban' else 'unban'}_user", target=target.username)
            flash(f"{'Banned' if action=='ban' else 'Unbanned'} {target.username}", 'info')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/roles/add', methods=['POST'])
def admin_add_role():
    guard = require_admin()
    if guard:
        return guard
    name = (request.form.get('role_name') or '').strip().lower()
    if not name:
        return redirect(url_for('admin'))
    db = locker_session()
    try:
        if not db.query(Role).filter(Role.name == name).first():
            db.add(Role(name=name, permissions=pyjson.dumps([])))
            db.commit()
            log_action(get_current_user().id, 'add_role', target=name)
            flash(f'Added role {name}', 'success')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/channel/add', methods=['POST'])
def admin_add_channel():
    guard = require_admin()
    if guard:
        return guard
    name = (request.form.get('name') or '').strip()
    slug = (request.form.get('slug') or '').strip().lower()
    if not name or not slug:
        return redirect(url_for('admin'))
    db = locker_session()
    try:
        if not db.query(Channel).filter(Channel.slug == slug).first():
            db.add(Channel(name=name, slug=slug, position=0, is_active=True, is_locked=False, is_hidden=False))
            db.commit()
            log_action(get_current_user().id, 'add_channel', target=slug)
            flash('Channel created.', 'success')
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/channel/toggle', methods=['POST'])
def admin_toggle_channel():
    guard = require_admin()
    if guard:
        return guard
    channel_id = request.form.get('channel_id')
    field = request.form.get('field')
    db = locker_session()
    try:
        ch = db.query(Channel).get(channel_id)
        if ch:
            if field == 'lock':
                ch.is_locked = not ch.is_locked
            if field == 'hide':
                ch.is_hidden = not ch.is_hidden
            db.commit()
            log_action(get_current_user().id, f'toggle_channel_{field}', target=ch.slug)
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/message/pin', methods=['POST'])
def admin_pin_message():
    guard = require_admin()
    if guard:
        return guard
    mid = request.form.get('message_id')
    db = locker_session()
    try:
        msg = db.query(Message).get(mid)
        if msg:
            msg.is_pinned = not msg.is_pinned
            db.commit()
            log_action(get_current_user().id, 'pin_message', target=str(mid), detail=str(msg.is_pinned))
    finally:
        db.close()
    return redirect(request.referrer or url_for('locker'))


@app.route('/admin/event/add', methods=['POST'])
def admin_add_event():
    guard = require_admin()
    if guard:
        return guard
    title = (request.form.get('title') or '').strip()
    location = (request.form.get('location') or '').strip()
    description = (request.form.get('description') or '').strip()
    date_raw = request.form.get('date')
    dt_val = None
    try:
        if date_raw:
            dt_val = datetime.fromisoformat(date_raw)
    except Exception:
        dt_val = None
    if not title:
        return redirect(url_for('admin'))
    db = locker_session()
    try:
        ev = Event(title=title, location=location, description=description, date=dt_val, created_at=datetime.utcnow())
        db.add(ev)
        db.commit()
        log_action(get_current_user().id, 'add_event', target=title)
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/event/delete', methods=['POST'])
def admin_delete_event():
    guard = require_admin()
    if guard:
        return guard
    event_id = request.form.get('event_id')
    db = locker_session()
    try:
        ev = db.query(Event).get(event_id)
        if ev:
            title = ev.title
            db.delete(ev)
            db.commit()
            log_action(get_current_user().id, 'delete_event', target=title)
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/badge/add', methods=['POST'])
def admin_add_badge():
    guard = require_admin()
    if guard:
        return guard
    name = (request.form.get('name') or '').strip()
    icon = (request.form.get('icon') or '').strip()
    desc = (request.form.get('description') or '').strip()
    if not name:
        return redirect(url_for('admin'))
    db = locker_session()
    try:
        if not db.query(Badge).filter(Badge.name == name).first():
            db.add(Badge(name=name, icon=icon, description=desc))
            db.commit()
            log_action(get_current_user().id, 'add_badge', target=name)
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/badge/delete', methods=['POST'])
def admin_delete_badge():
    guard = require_admin()
    if guard:
        return guard
    badge_id = request.form.get('badge_id')
    db = locker_session()
    try:
        b = db.query(Badge).get(badge_id)
        if b:
            name = b.name
            db.delete(b)
            db.commit()
            log_action(get_current_user().id, 'delete_badge', target=name)
    finally:
        db.close()
    return redirect(url_for('admin'))


@app.route('/admin/badge/award', methods=['POST'])
def admin_award_badge():
    guard = require_admin()
    if guard:
        return guard
    badge_id = request.form.get('badge_id')
    user_id = request.form.get('user_id')
    db = locker_session()
    try:
        if badge_id and user_id:
            db.add(UserBadge(user_id=user_id, badge_id=badge_id))
            db.commit()
            log_action(get_current_user().id, 'award_badge', target=user_id, detail=badge_id)
            flash('Badge awarded.', 'success')
    finally:
        db.close()
    return redirect(url_for('admin'))
@app.route('/get-started')
def get_started():
    if not get_current_user():
        return redirect(url_for('login'))
    return render_template('get_started.html')


@app.route('/settings')
def settings():
    user = get_current_user()
    if not user:
        return redirect(url_for('login', next=url_for('settings')))
    faq = [
        ("How do I change my profile picture?", "Go to your profile page and click your avatar to upload a new picture."),
        ("How do I update my nickname or bio?", "On your profile, use the Change button for nickname or the Edit bio link."),
        ("How do I link my player to LAB defaults?", "On your profile, select your player from the dropdown; LAB tools will default to it."),
        ("How do I log out?", "Use the person icon in the top bar and select Logout."),
        ("Who can see my messages?", "Locker messages are visible to anyone visiting the Locker page.")
    ]
    return render_template('settings.html', user=user, faq=faq)

# --- Developer mode toggle (set True for strict Jinja2, False for production) ---


# --- Persistent Developer Mode Config ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
def load_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"developer_mode": False}

def save_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)

config = load_config()
app.config['DEVELOPER_MODE'] = config.get('developer_mode', False)
if app.config['DEVELOPER_MODE']:
    from jinja2 import StrictUndefined
    app.jinja_env.undefined = StrictUndefined
# --- Schedule route ---

# --- Admin route with developer mode toggle ---
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    guard = require_admin()
    if guard:
        return render_template('admin_denied.html'), 302
    db = locker_session()
    users = []
    roles = []
    recent_messages = []
    recent_comments = []
    audit_entries = []
    channels = []
    events = []
    badges = []
    message = None
    message_type = None
    developer_mode = app.config.get('DEVELOPER_MODE', False)
    allowed_files = [LEAGUE_DATA_FILENAME, 'league_data_final_week11.json', 'schedule.json', 'dues.json', 'apa_data_2025-12-03_update1.json', 'apa_data_converted.json']
    badge_requirements = {}
    ai_logs = []
    latest_dump = find_latest_dump()
    latest_dump_display = None
    latest_backup = find_latest_backup()
    latest_backup_display = None
    if latest_dump:
        try:
            latest_dump_display = latest_dump.relative_to(Path(DATA_DIR)).as_posix()
        except Exception:
            latest_dump_display = str(latest_dump)
    if latest_backup:
        try:
            latest_backup_display = latest_backup.relative_to(Path(DATA_DIR)).as_posix()
        except Exception:
            latest_backup_display = str(latest_backup)
    try:
        users = db.query(User).order_by(User.username).all()
        roles = db.query(Role).order_by(Role.name).all()
        recent_messages = db.query(Message).order_by(Message.created_at.desc()).limit(10).all()
        recent_comments = db.query(ProfileComment).order_by(ProfileComment.created_at.desc()).limit(10).all()
        audit_entries = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(15).all()
        channels = db.query(Channel).order_by(Channel.position, Channel.name).all()
        events = db.query(Event).order_by(Event.date.desc().nullslast()).all()
        badges = db.query(Badge).order_by(Badge.name).all()
        from sqlalchemy.orm import joinedload
        ai_logs_query = (
            db.query(AIChat)
            .options(joinedload(AIChat.user))
            .order_by(AIChat.created_at.desc())
            .limit(50)
            .all()
        )
        ai_logs = []
        for log in ai_logs_query:
            if log.user:
                user_label = log.user.display_name or log.user.username
            elif log.user_id:
                user_label = f"User {log.user_id}"
            else:
                user_label = 'Unknown'
            ai_logs.append({
                "created_at": log.created_at,
                "user_label": user_label,
                "question": log.question,
                "answer": log.answer,
            })
        badge_requirements = {
            "Locker Rookie": "First activity in the Locker",
            "First Message": "Post your first message",
            "Memes Dealer": "10+ posts in memes/media channels",
            "Shot Caller": "5+ posts in strategies channel",
            "Clown Shoes": "15+ memes/media posts",
            "Media Dropper": "5+ media/highlights posts",
            "Uploader Pro": "10+ media/highlights uploads",
            "Hype Master": "15+ posts in general/announcements",
            "Hyperactive": "Post in 4+ distinct channels",
            "Channel Hopper": "Use 6+ distinct channels",
            "Keyboard Warrior": "100+ total messages",
            "Deep Thinker": "Long posts (200+ chars)",
            "Story Mode": "Very long posts (400+ chars)",
            "Emoji Addict": "Messages packed with emojis",
            "Vibe Setter": "30+ total messages",
            "Night Owl": "Post between 12am–5am",
            "Early Bird": "Post between 5am–9am",
            "Theme Lord": "Set accent or background",
            "Glow Architect": "Custom accent color",
            "Wallpaper Wizard": "Custom profile background",
            "Audiophile": "Set a theme song",
            "Playlist Curator": "Add multiple theme songs",
            "Bio Poet": "Bio length 120+ characters",
            "Mood Setter": "Set your mood/status",
            "Design Freak": "Update nickname/theme",
            # Defaults for manual awards
        }
    except Exception as e:
        logging.error(f'Admin load error: {e}')
    if request.method == 'POST':
        password = request.form.get('admin_pass', '')
        if password != ADMIN_PASS:
            message = 'Incorrect password.'
            message_type = 'danger'
        else:
            dev_mode_toggle = request.form.get('dev_mode_toggle') == '1'
            # Update config and persist
            app.config['DEVELOPER_MODE'] = dev_mode_toggle
            config = load_config()
            config['developer_mode'] = dev_mode_toggle
            save_config(config)
            # Update Jinja2 mode immediately
            if dev_mode_toggle:
                from jinja2 import StrictUndefined
                app.jinja_env.undefined = StrictUndefined
            else:
                from jinja2 import Undefined
                app.jinja_env.undefined = Undefined
            developer_mode = dev_mode_toggle
            message = 'Developer mode updated.'
            message_type = 'success'
            # Handle JSON upload if provided
            file = request.files.get('json_file')
            if file and file.filename:
                filename = secure_filename(file.filename)
                ext = filename.rsplit('.', 1)[-1].lower()
                target_file = request.form.get('target_file') or LEAGUE_DATA_FILENAME
                if target_file not in allowed_files:
                    message = f'Target file "{target_file}" not allowed. Choose one of: {", ".join(allowed_files)}.'
                    message_type = 'danger'
                elif ext not in ALLOWED_EXTENSIONS:
                    message = 'Invalid file type. Only JSON allowed.'
                    message_type = 'danger'
                else:
                    target_path = os.path.join(DATA_DIR, target_file)
                    try:
                        with LOCK:
                            file.save(target_path)
                        message = f'Uploaded and replaced {target_file}.'
                        message_type = 'success'
                    except Exception as e:
                        logging.error(f'Error saving uploaded file: {e}')
                        message = 'Failed to save file.'
                        message_type = 'danger'
            elif message_type is None:
                # No file uploaded, only dev mode change
                message_type = 'info'
    db.close()
    return render_template(
        'admin.html',
        developer_mode=developer_mode,
        message=message,
        message_type=message_type,
        allowed_files=allowed_files,
        users=users,
        roles=roles,
        recent_messages=recent_messages,
        recent_comments=recent_comments,
        audit_entries=audit_entries,
        channels=channels,
        events=events,
        badges=badges
        , badge_requirements=badge_requirements
        , latest_dump=latest_dump_display
        , latest_backup=latest_backup_display
        , ai_logs=ai_logs
    )

@app.route('/admin/update-data', methods=['POST'])
def admin_update_data():
    guard = require_admin()
    if guard:
        return guard
    dump_path_raw = request.form.get('dump_path')
    dump_path = Path(dump_path_raw) if dump_path_raw else find_latest_dump()
    if dump_path_raw and not dump_path.is_absolute():
        dump_path = Path(DATA_DIR) / dump_path
    if not dump_path or not dump_path.exists():
        flash('No API dump found to update data.', 'danger')
        return redirect(url_for('admin'))
    script_path = Path(__file__).parent / 'scripts' / 'update_from_dump.py'
    target_path = Path(DATA_DIR) / LEAGUE_DATA_FILENAME
    if not script_path.exists():
        flash('Update script missing. Cannot refresh data.', 'danger')
        return redirect(url_for('admin'))
    cmd = [
        sys.executable, str(script_path),
        '--dump', str(dump_path),
        '--target', str(target_path),
        '--backup-dir', str(Path(DATA_DIR) / 'backups')
    ]
    try:
        auto_backup(LEAGUE_DATA_FILENAME)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        if result.returncode == 0:
            flash(f'League data updated from {dump_path.name}.', 'success')
            log_action(get_current_user().id, 'update_data', target=target_path.name, detail=dump_path.name)
            if stdout:
                flash(stdout.splitlines()[-1], 'info')
        else:
            flash('Data update failed. Check logs/output.', 'danger')
            if stderr:
                flash(stderr.splitlines()[-1], 'warning')
    except subprocess.TimeoutExpired:
        flash('Data update timed out.', 'danger')
    except Exception as e:
        logging.error(f'admin_update_data error: {e}')
        flash('Unexpected error during data update.', 'danger')

@app.route('/admin/update-data-json', methods=['POST'])
def admin_update_data_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    dump_path_raw = request.form.get('dump_path') or None
    dump_path = Path(dump_path_raw) if dump_path_raw else find_latest_dump()
    if dump_path_raw and not dump_path.is_absolute():
        dump_path = Path(DATA_DIR) / dump_path
    if not dump_path or not dump_path.exists():
        return jsonify({"ok": False, "error": "No API dump found to update data."}), 400
    script_path = Path(__file__).parent / 'scripts' / 'update_from_dump.py'
    target_path = Path(DATA_DIR) / LEAGUE_DATA_FILENAME
    if not script_path.exists():
        return jsonify({"ok": False, "error": "Update script missing."}), 400
    cmd = [
        sys.executable, str(script_path),
        '--dump', str(dump_path),
        '--target', str(target_path),
        '--backup-dir', str(Path(DATA_DIR) / 'backups')
    ]
    logs = []
    try:
        auto_backup(LEAGUE_DATA_FILENAME)
        ok, out = run_script_with_output_collect('Refresh data from dump', cmd, timeout=180)
        logs.extend(out)
        if ok:
            log_action(get_current_user().id, 'update_data', target=target_path.name, detail=dump_path.name)
            return jsonify({"ok": True, "msg": f"League data updated from {dump_path.name}.", "logs": logs})
        return jsonify({"ok": False, "error": "Data update failed. Check logs/output.", "logs": logs}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Data update timed out."}), 500
    except Exception as e:
        logging.error(f'admin_update_data_json error: {e}')
        return jsonify({"ok": False, "error": "Unexpected error during data update."}), 500
    return redirect(url_for('admin', _anchor='data-section'))

@app.route('/admin/run-pipeline', methods=['POST'])
def admin_run_pipeline():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'pipeline.py'
    if not script_path.exists():
        flash('Update pipeline script missing.', 'danger')
        return redirect(url_for('admin'))
    cmd = [sys.executable, str(script_path)]
    ok = run_script_with_output('Full Update (pipeline + merge)', cmd, cwd=script_path.parent, timeout=300)
    if ok:
        log_action(get_current_user().id, 'run_pipeline', target='pipeline')
        flash('Updating league data from latest dump...', 'info')
        latest_dump = find_latest_dump()
        if latest_dump and latest_dump.exists():
            _run_update_from_dump(latest_dump)
        else:
            flash('No latest dump found after pipeline run.', 'warning')
        # Chain decode -> merge copy -> apply to main
        decode_script = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'decode_dump.py'
        merge_script = Path(__file__).parent / 'scripts' / 'merge_decoded_matches.py'
        if decode_script.exists():
            run_script_with_output('Decode latest dump', [sys.executable, str(decode_script)], cwd=decode_script.parent, timeout=300)
        if merge_script.exists():
            run_script_with_output('Merge decoded matches (copy only)', [sys.executable, str(merge_script)], cwd=merge_script.parent, timeout=300)
        # Apply merged to main with backup
        base_dir = Path(DATA_DIR)
        main = base_dir / LEAGUE_DATA_FILENAME
        merged = base_dir / 'merge' / 'league_with_matches.json'
        if merged.exists():
            backup_dir = base_dir / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{main.stem}_auto_apply.bak.json"
            try:
                shutil.copy2(main, backup_path)
                data = json.load(merged.open())
                main.write_text(json.dumps(data, indent=2), encoding='utf-8')
                flash(f'Auto-applied merged matches to main. Backup: {backup_path.name}', 'success')
                log_action(get_current_user().id, 'apply_merged', target=main.name, detail=backup_path.name)
            except Exception as e:
                logging.error(f'auto apply merged error: {e}')
                flash('Failed to auto-apply merged data.', 'danger')
    return redirect(url_for('admin', _anchor='scripts-section'))

@app.route('/admin/run-pipeline-json', methods=['POST'])
def admin_run_pipeline_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    logs = []
    def log(msg): logs.append(msg)
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'pipeline.py'
    if not script_path.exists():
        return jsonify({"error": "pipeline missing"}), 400
    ok, out = run_script_with_output_collect('Pipeline', [sys.executable, str(script_path)], cwd=script_path.parent, timeout=300)
    logs.extend(out)
    if ok:
        # merge update_from_dump
        latest_dump = find_latest_dump()
        if latest_dump and latest_dump.exists():
            _run_update_from_dump(latest_dump)
            logs.append(f'update_from_dump applied from {latest_dump.name}')
        else:
            logs.append('No latest dump found after pipeline.')
        # decode
        decode_script = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'decode_dump.py'
        if decode_script.exists():
            ok2, out2 = run_script_with_output_collect('Decode latest dump', [sys.executable, str(decode_script)], cwd=decode_script.parent, timeout=300)
            logs.extend(out2)
        # merge copy
        merge_script = Path(__file__).parent / 'scripts' / 'merge_decoded_matches.py'
        if merge_script.exists():
            ok3, out3 = run_script_with_output_collect('Merge decoded matches (copy only)', [sys.executable, str(merge_script)], cwd=merge_script.parent, timeout=300)
            logs.extend(out3)
        # apply merged to main
        base_dir = Path(DATA_DIR)
        main = base_dir / LEAGUE_DATA_FILENAME
        merged = base_dir / 'merge' / 'league_with_matches.json'
        if merged.exists():
            backup_dir = base_dir / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{main.stem}_auto_apply.bak.json"
            try:
                shutil.copy2(main, backup_path)
                data = json.load(merged.open())
                main.write_text(json.dumps(data, indent=2), encoding='utf-8')
                logs.append(f'Applied merged to main. Backup: {backup_path.name}')
            except Exception as e:
                logs.append(f'Failed to apply merged: {e}')
    return jsonify({"ok": ok, "logs": logs})

@app.route('/admin/run-pipeline-only', methods=['POST'])
def admin_run_pipeline_only():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'pipeline.py'
    if not script_path.exists():
        flash('Update pipeline script missing.', 'danger')
        return redirect(url_for('admin'))
    cmd = [sys.executable, str(script_path)]
    ok = run_script_with_output('Pipeline (collect latest dump, no merge)', cmd, cwd=script_path.parent, timeout=300)
    if ok:
        log_action(get_current_user().id, 'run_pipeline_only', target='pipeline')
    return redirect(url_for('admin', _anchor='scripts-section'))

@app.route('/admin/run-url-discovery', methods=['POST'])
def admin_run_url_discovery():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'apa_url_discovery.py'
    if not script_path.exists():
        flash('URL discovery script missing.', 'danger')
        return redirect(url_for('admin'))
    cmd = [sys.executable, str(script_path)]
    run_script_with_output('URL Discovery', cmd, cwd=script_path.parent, timeout=300)
    return redirect(url_for('admin', _anchor='scripts-section'))

@app.route('/admin/run-api-sniffer', methods=['POST'])
def admin_run_api_sniffer():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'apa_api_sniffer.py'
    if not script_path.exists():
        flash('API sniffer script missing.', 'danger')
        return redirect(url_for('admin'))
    cmd = [sys.executable, str(script_path)]
    run_script_with_output('API Sniffer', cmd, cwd=script_path.parent, timeout=300)
    return redirect(url_for('admin', _anchor='scripts-section'))

@app.route('/admin/decode-dump', methods=['POST'])
def admin_decode_dump():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'decode_dump.py'
    if not script_path.exists():
        flash('Decoder script missing.', 'danger')
        return redirect(url_for('admin', _anchor='data-section'))
    cmd = [sys.executable, str(script_path)]
    run_script_with_output('Decode latest dump', cmd, cwd=script_path.parent, timeout=300)
    return redirect(url_for('admin', _anchor='data-section'))

@app.route('/admin/decode-dump-json', methods=['POST'])
def admin_decode_dump_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'decode_dump.py'
    if not script_path.exists():
        return jsonify({"error": "decoder missing"}), 400
    ok, logs = run_script_with_output_collect('Decode latest dump', [sys.executable, str(script_path)], cwd=script_path.parent, timeout=300)
    return jsonify({"ok": ok, "logs": logs})

@app.route('/admin/merge-decoded', methods=['POST'])
def admin_merge_decoded():
    guard = require_admin()
    if guard:
        return guard
    script_path = Path(__file__).parent / 'scripts' / 'merge_decoded_matches.py'
    if not script_path.exists():
        flash('Merge script missing.', 'danger')
        return redirect(url_for('admin', _anchor='data-section'))
    cmd = [sys.executable, str(script_path)]
    run_script_with_output('Merge decoded matches (copy only)', cmd, cwd=script_path.parent, timeout=180)
    return redirect(url_for('admin', _anchor='data-section'))

@app.route('/admin/merge-decoded-json', methods=['POST'])
def admin_merge_decoded_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'merge_decoded_matches.py'
    if not script_path.exists():
        return jsonify({"error": "merge script missing"}), 400
    ok, logs = run_script_with_output_collect('Merge decoded matches (copy only)', [sys.executable, str(script_path)], cwd=script_path.parent, timeout=180)
    return jsonify({"ok": ok, "logs": logs})

@app.route('/admin/apply-merged', methods=['POST'])
def admin_apply_merged():
    guard = require_admin()
    if guard:
        return guard
    base_dir = Path(DATA_DIR)
    main = base_dir / LEAGUE_DATA_FILENAME
    merged = base_dir / 'merge' / 'league_with_matches.json'
    if not merged.exists():
        flash('Merged file not found. Run merge decoded matches first.', 'warning')
        return redirect(url_for('admin', _anchor='data-section'))
    backup_dir = base_dir / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{main.stem}_merged_apply.bak.json"
    try:
        shutil.copy2(main, backup_path)
        data = json.load(merged.open())
        main.write_text(json.dumps(data, indent=2), encoding='utf-8')
        flash(f'Applied merged matches to main data. Backup saved to {backup_path.name}', 'success')
        log_action(get_current_user().id, 'apply_merged', target=main.name, detail=backup_path.name)
    except Exception as e:
        logging.error(f'apply merged error: {e}')
        flash('Failed to apply merged data.', 'danger')
    return redirect(url_for('admin', _anchor='data-section'))

@app.route('/admin/apply-merged-json', methods=['POST'])
def admin_apply_merged_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    base_dir = Path(DATA_DIR)
    main = base_dir / LEAGUE_DATA_FILENAME
    merged = base_dir / 'merge' / 'league_with_matches.json'
    if not merged.exists():
        return jsonify({"error": "merged file missing"}), 400
    backup_dir = base_dir / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{main.stem}_merged_apply.bak.json"
    try:
        shutil.copy2(main, backup_path)
        data = json.load(merged.open())
        main.write_text(json.dumps(data, indent=2), encoding='utf-8')
        return jsonify({"ok": True, "backup": backup_path.name, "msg": "applied merged to main"})
    except Exception as e:
        logging.error(f'apply merged json error: {e}')
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/admin/merge-membership-json', methods=['POST'])
def admin_merge_membership_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'merge_membership_years.py'
    if not script_path.exists():
        return jsonify({"ok": False, "error": "merge_membership_years.py missing"}), 400
    ok, logs = run_script_with_output_collect(
        'Merge membership years',
        [sys.executable, str(script_path)],
        cwd=script_path.parent,
        timeout=180
    )
    return jsonify({"ok": ok, "logs": logs, "msg": "Merged membership years (backup created)" if ok else "Merge failed"})

@app.route('/admin/merge-session-highlights-json', methods=['POST'])
def admin_merge_session_highlights_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'merge_session_highlights.py'
    if not script_path.exists():
        return jsonify({"ok": False, "error": "merge_session_highlights.py missing"}), 400
    ok, logs = run_script_with_output_collect(
        'Merge session highlights',
        [sys.executable, str(script_path)],
        cwd=script_path.parent,
        timeout=180
    )
    return jsonify({"ok": ok, "logs": logs, "msg": "Merged session highlights (backup created)" if ok else "Merge failed"})

@app.route('/admin/merge-lifetime-json', methods=['POST'])
def admin_merge_lifetime_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'merge_lifetime_extended.py'
    if not script_path.exists():
        return jsonify({"ok": False, "error": "merge_lifetime_extended.py missing"}), 400
    ok, logs = run_script_with_output_collect(
        'Merge extended lifetime stats',
        [sys.executable, str(script_path)],
        cwd=script_path.parent,
        timeout=180
    )
    return jsonify({"ok": ok, "logs": logs, "msg": "Merged lifetime stats (backup created)" if ok else "Merge failed"})

@app.route('/admin/merge-membership-meta-json', methods=['POST'])
def admin_merge_membership_meta_json():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    script_path = Path(__file__).parent / 'scripts' / 'merge_membership_meta.py'
    if not script_path.exists():
        return jsonify({"ok": False, "error": "merge_membership_meta.py missing"}), 400
    ok, logs = run_script_with_output_collect(
        'Merge membership meta',
        [sys.executable, str(script_path)],
        cwd=script_path.parent,
        timeout=180
    )
    return jsonify({"ok": ok, "logs": logs, "msg": "Merged membership meta (backup created)" if ok else "Merge failed"})

@app.route('/admin/git/push', methods=['POST'])
def admin_git_push():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    repo_dir = Path(__file__).resolve().parent
    logs = []
    try:
        for cmd in [["git", "status", "--short"], ["git", "push"]]:
            proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=60)
            logs.append(f"$ {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
            if proc.returncode != 0 and "nothing to commit" not in proc.stdout.lower():
                return jsonify({"ok": False, "error": f"Command failed: {' '.join(cmd)}", "logs": logs}), 500
        return jsonify({"ok": True, "logs": logs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "logs": logs}), 500

@app.route('/admin/git/commit', methods=['POST'])
def admin_git_commit():
    guard = require_admin()
    if guard:
        return jsonify({"error": "unauthorized"}), 401
    repo_dir = Path(__file__).resolve().parent
    message = (request.form.get('message') or '').strip()
    if not message:
        user = get_current_user()
        username = user.username if user else 'admin'
        message = f"Admin commit by {username} at {datetime.now(timezone.utc).isoformat()}"
    logs = []
    try:
        for cmd in [["git", "add", "-A"], ["git", "commit", "-m", message]]:
            proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=60)
            logs.append(f"$ {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
            if proc.returncode != 0:
                output = proc.stdout + proc.stderr
                if "nothing to commit" in output.lower():
                    return jsonify({"ok": False, "error": "nothing to commit", "logs": logs}), 400
                return jsonify({"ok": False, "error": f"Command failed: {' '.join(cmd)}", "logs": logs}), 500
        log_action(get_current_user().id, 'git_commit', target='repo', detail=message)
        return jsonify({"ok": True, "logs": logs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "logs": logs}), 500

@app.route('/admin/restore-backup', methods=['POST'])
def admin_restore_backup():
    guard = require_admin()
    if guard:
        return guard
    target_path = Path(DATA_DIR) / LEAGUE_DATA_FILENAME
    backup_path = find_latest_backup()
    if not backup_path or not backup_path.exists():
        flash('No backup found to restore.', 'warning')
        return redirect(url_for('admin'))
    try:
        shutil.copy2(backup_path, target_path)
        flash(f'Restored {target_path.name} from backup {backup_path.name}', 'success')
        log_action(get_current_user().id, 'restore_backup', target=target_path.name, detail=backup_path.name)
    except Exception as e:
        logging.error(f'Backup restore error: {e}')
        flash('Failed to restore backup.', 'danger')
    return redirect(url_for('admin', _anchor='data-section'))

@app.route('/apa-manual')
def apa_manual():
    """Serve the APA rules & scoring reference."""
    manual_path = Path(__file__).parent / 'APA_RULES_AND_SCORING.txt'
    if not manual_path.exists():
        abort(404)
    return send_from_directory(manual_path.parent, manual_path.name, as_attachment=False)

def _serialize_chat(chat: AIChat) -> dict:
    return {
        "id": chat.id,
        "question": chat.question,
        "answer": chat.answer,
        "created_at": chat.created_at.isoformat() if chat.created_at else None,
        "thread_id": chat.thread_id,
    }

def _serialize_thread(thread: AIThread) -> dict:
    return {
        "id": thread.id,
        "title": thread.title or f"Chat {thread.id}",
        "updated_at": thread.updated_at.isoformat() if thread.updated_at else None,
        "is_archived": bool(thread.is_archived),
    }

def _get_or_create_thread(user, db, thread_id=None, title=None):
    """Return a valid thread for the user, creating one if needed."""
    thread = None
    if thread_id:
        thread = db.query(AIThread).filter(
            AIThread.id == thread_id,
            AIThread.user_id == user.id,
            or_(AIThread.is_archived == False, AIThread.is_archived.is_(None))
        ).first()
    if not thread and session.get('ai_thread_id'):
        tid = session.get('ai_thread_id')
        thread = db.query(AIThread).filter(
            AIThread.id == tid,
            AIThread.user_id == user.id,
            or_(AIThread.is_archived == False, AIThread.is_archived.is_(None))
        ).first()
    if not thread:
        thread = (
            db.query(AIThread)
            .filter(AIThread.user_id == user.id, or_(AIThread.is_archived == False, AIThread.is_archived.is_(None)))
            .order_by(AIThread.updated_at.desc().nullslast(), AIThread.id.desc())
            .first()
        )
    if not thread:
        count = db.query(func.count(AIThread.id)).filter(AIThread.user_id == user.id).scalar() or 0
        thread = AIThread(user_id=user.id, title=title or f"Chat {count + 1}")
        db.add(thread)
        db.commit()
    session['ai_thread_id'] = thread.id
    return thread

def _thread_messages(db, thread_id, user_id, limit=50):
    return (
        db.query(AIChat)
        .filter(AIChat.user_id == user_id, AIChat.thread_id == thread_id)
        .order_by(AIChat.created_at.asc())
        .limit(limit)
        .all()
    )

@app.route('/ai/ask', methods=['POST'])
def ai_ask():
    user = get_current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    model = 'phi3:mini'  # lock to fast local default
    requested_thread = data.get('thread_id')
    if not question:
        return jsonify({"error": "question required"}), 400
    db = locker_session()
    try:
        thread = _get_or_create_thread(user, db, thread_id=requested_thread)
        recent_chats = (
            db.query(AIChat)
            .filter(AIChat.user_id == user.id, AIChat.thread_id == thread.id)
            .order_by(AIChat.created_at.desc())
            .limit(12)
            .all()
        )
        history_lines = []
        for chat in reversed(recent_chats):
            history_lines.append(f"Q: {chat.question}")
            if chat.answer:
                history_lines.append(f"A: {chat.answer}")
        history_text = "\n".join(history_lines)
        context = build_ai_context(user)
        prompt = (
            "You are Cue-Ligans' local stats assistant. Be concise (2–4 sentences max) and direct.\n"
            "If the question is ambiguous (e.g., 'what rank am I?'), ask one short clarifier "
            "like 'Do you mean skill level in 8-ball, 9-ball, or division standings?'.\n"
            "If data is missing, say so. Do not invent stats.\n\n"
            f"Recent conversation (most recent last, truncated):\n{history_text or 'None'}\n\n"
            f"Context (trimmed):\n{context}\n\nQuestion: {question}\nAnswer:"
        )
        answer = call_ollama(prompt, model=model)
        if not answer:
            return jsonify({"error": "AI unavailable"}), 500
        if not thread.title:
            thread.title = question[:80] or f"Chat {thread.id}"
        thread.updated_at = datetime.utcnow()
        chat = AIChat(user_id=user.id, question=question, answer=answer, thread_id=thread.id, created_at=datetime.utcnow())
        db.add(chat)
        db.commit()
        session['ai_thread_id'] = thread.id
        messages = _thread_messages(db, thread.id, user.id)
        serialized_messages = [_serialize_chat(c) for c in messages]
        threads = (
            db.query(AIThread)
            .filter(AIThread.user_id == user.id, or_(AIThread.is_archived == False, AIThread.is_archived.is_(None)))
            .order_by(AIThread.updated_at.desc().nullslast(), AIThread.id.desc())
            .all()
        )
        serialized_threads = [_serialize_thread(t) for t in threads]
    finally:
        db.close()
    return jsonify({
        "answer": answer,
        "thread_id": thread.id,
        "thread_title": thread.title,
        "messages": serialized_messages,
        "threads": serialized_threads
    })

@app.route('/ai/reset', methods=['POST'])
def ai_reset():
    user = get_current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    db = locker_session()
    try:
        count = db.query(func.count(AIThread.id)).filter(AIThread.user_id == user.id).scalar() or 0
        new_thread = AIThread(user_id=user.id, title=f"Chat {count + 1}")
        db.add(new_thread)
        db.commit()
        session['ai_thread_id'] = new_thread.id
        threads = (
            db.query(AIThread)
            .filter(AIThread.user_id == user.id, or_(AIThread.is_archived == False, AIThread.is_archived.is_(None)))
            .order_by(AIThread.updated_at.desc().nullslast(), AIThread.id.desc())
            .all()
        )
        serialized_threads = [_serialize_thread(t) for t in threads]
    finally:
        db.close()
    return jsonify({
        "ok": True,
        "message": "Started a new chat. Previous chats are still saved.",
        "thread_id": new_thread.id,
        "thread_title": new_thread.title,
        "messages": [],
        "threads": serialized_threads
    })

@app.route('/ai/thread/<int:thread_id>', methods=['GET'])
def ai_thread(thread_id: int):
    user = get_current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    db = locker_session()
    try:
        thread = db.query(AIThread).filter(
            AIThread.id == thread_id,
            AIThread.user_id == user.id,
            or_(AIThread.is_archived == False, AIThread.is_archived.is_(None))
        ).first()
        if not thread:
            return jsonify({"error": "not found"}), 404
        session['ai_thread_id'] = thread.id
        messages = _thread_messages(db, thread.id, user.id, limit=200)
        serialized_messages = [_serialize_chat(m) for m in messages]
        return jsonify({
            "thread_id": thread.id,
            "thread_title": thread.title,
            "messages": serialized_messages
        })
    finally:
        db.close()

@app.route('/coach/planner', methods=['GET', 'POST'])
def coach_planner():
    user = get_current_user()
    if not is_staff(user):
        abort(403)
    league = load_league_data()
    divisions = league.get('divisions', {}) or {}
    teams = league.get('teams', {}) or {}
    players = league.get('players', {}) or {}
    matches = league.get('matches', {}) or {}
    formats = sorted({(d.get('type') or d.get('format') or '').lower() for d in divisions.values() if (d.get('type') or d.get('format'))})
    selected_fmt = (request.values.get('format') or (formats[0] if formats else '8-ball')).lower()
    divisions_fmt = [(did, d) for did, d in divisions.items() if (d.get('type') or d.get('format') or '').lower() == selected_fmt]
    selected_div = request.values.get('division') or (divisions_fmt[0][0] if divisions_fmt else '')
    cue8, cue9 = find_cue_team_ids(league)
    my_team_id = cue8 if '8' in selected_fmt else cue9

    # Weeks/opponents from schedule/matches
    weeks = []
    opponents_by_week = {}
    for mid, m in matches.items():
        if str(m.get('division_id')) != str(selected_div):
            continue
        if m.get('home_team_id') == my_team_id or m.get('away_team_id') == my_team_id:
            wk = m.get('week') or m.get('match_week') or m.get('date') or f"Match {mid}"
            weeks.append(wk)
            opp_id = m.get('home_team_id') if m.get('away_team_id') == my_team_id else m.get('away_team_id')
            opponents_by_week.setdefault(wk, []).append(opp_id)
    weeks = list(dict.fromkeys(weeks))
    selected_week = request.values.get('week') or (weeks[0] if weeks else '')
    opp_list = opponents_by_week.get(selected_week, [])
    selected_opp = request.values.get('opponent') or (opp_list[0] if opp_list else '')

    def roster_for_team(tid):
        roster = []
        team = teams.get(tid) or {}
        for entry in team.get('roster', []):
            pid = entry.get('player_id')
            if not pid:
                continue
            pdata = players.get(pid) or {}
            sl = None
            csl = pdata.get('current_skill_levels') or {}
            sl = csl.get(selected_fmt) or csl.get(selected_fmt.replace('-','_'))
            # sessions may be stored as dict keyed by format or as a list of blocks
            sessions_obj = pdata.get('sessions') or {}
            if isinstance(sessions_obj, list):
                sess = next((s for s in sessions_obj if (s.get('format') or '').lower() == selected_fmt), {})  # type: ignore
            else:
                sess = sessions_obj.get(selected_fmt) or sessions_obj.get(selected_fmt.replace('-','_'), {})
            stats = {
                'ppm': sess.get('points_per_match') or sess.get('ppm'),
                'pa': sess.get('percent_points_avail') or sess.get('pa'),
                'win_pct': sess.get('win_pct'),
                'matches_played': sess.get('matches_played') or sess.get('matchesPlayed')
            }
            roster.append({
                'id': pid,
                'name': pdata.get('full_name') or pdata.get('short_name') or pid,
                'sl': sl,
                'stats': stats,
                'team_id': tid
            })
        return roster

    my_roster = roster_for_team(my_team_id) if my_team_id else []
    opp_roster = roster_for_team(selected_opp) if selected_opp else []
    opp_roster_sorted = sorted(opp_roster, key=lambda x: (x.get('sl') or 0, x.get('name')))

    plans = load_planned_lineups()
    plan_key = f"{selected_fmt}|{selected_div}|{selected_week}|{selected_opp}"
    if request.method == 'POST':
        lineup = [request.form.get(f'slot_{i}', '') for i in range(1,6)]
        opp_assign = [request.form.get(f'opp_slot_{i}', '') for i in range(1,6)]
        notes = {f"slot_{i}": request.form.get(f'note_{i}', '') for i in range(1,6)}
        plans[plan_key] = {'lineup': lineup, 'opp_lineup': opp_assign, 'notes': notes}
        save_planned_lineups(plans)
        flash('Lineup saved for this week/opponent.', 'success')
        return redirect(url_for('coach_planner', format=selected_fmt, division=selected_div, week=selected_week, opponent=selected_opp))
    loaded_plan = plans.get(plan_key, {'lineup': ['','','','',''], 'opp_lineup': ['','','','',''], 'notes': {}})

    def team_summary(roster):
        if not roster:
            return {'avg_sl': '-', 'players': 0}
        sls = [r.get('sl') for r in roster if r.get('sl') is not None]
        avg = round(sum(sls)/len(sls),1) if sls else '-'
        return {'avg_sl': avg, 'players': len(roster)}

    # Build recent results (last 5) per player
    recent_results = {}
    for m in matches.values():
        sets = m.get('sets') or []
        for s in sets:
            if not isinstance(s, dict):
                continue
            for role_tag in [('home_player_id','home_points','away_points'), ('away_player_id','away_points','home_points')]:
                pid = s.get(role_tag[0])
                if not pid:
                    continue
                pts = s.get(role_tag[1])
                opp_pts = s.get(role_tag[2])
                res = 1 if pts is not None and opp_pts is not None and pts > opp_pts else (-1 if pts is not None and opp_pts is not None and pts < opp_pts else 0)
                recent_results.setdefault(str(pid), []).append(res)
    for k,v in recent_results.items():
        recent_results[k] = v[:5]

    return render_template(
        'coach_planner.html',
        selected_fmt=selected_fmt,
        formats=formats,
        divisions=divisions_fmt,
        selected_div=selected_div,
        weeks=weeks,
        selected_week=selected_week,
        opponents=opp_list,
        selected_opp=selected_opp,
        my_roster=my_roster,
        opp_roster=opp_roster_sorted,
        team_summary=team_summary(my_roster),
        opp_summary=team_summary(opp_roster_sorted),
        plan=loaded_plan,
        player_map=players,
        recent_results=recent_results
    )

@app.route('/coach/planner/save', methods=['POST'])
def coach_planner_save():
    user = get_current_user()
    if not is_staff(user):
        abort(403)
    payload = request.get_json(force=True, silent=True) or {}
    key = payload.get('plan_key')
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400
    plans = load_planned_lineups()
    plans[key] = {
        'lineup': payload.get('lineup') or ['','','','',''],
        'opp_lineup': payload.get('opp_lineup') or ['','','','',''],
        'notes': payload.get('notes') or {}
    }
    save_planned_lineups(plans)
    return jsonify({"ok": True})
@app.route('/ai', methods=['GET'])
def ai_page():
    user = get_current_user()
    if not user:
        return redirect(url_for('login', next=url_for('ai_page')))
    # Quick Ollama health check
    ollama_ok = False
    try:
        proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        ollama_ok = proc.returncode == 0
    except Exception:
        ollama_ok = False
    db = locker_session()
    try:
        threads = (
            db.query(AIThread)
            .filter(AIThread.user_id == user.id, or_(AIThread.is_archived == False, AIThread.is_archived.is_(None)))
            .order_by(AIThread.updated_at.desc().nullslast(), AIThread.id.desc())
            .all()
        )
        active_thread = None
        if session.get('ai_thread_id'):
            active_thread = next((t for t in threads if t.id == session.get('ai_thread_id')), None)
        if not active_thread and threads:
            active_thread = threads[0]
        # If no thread exists, create one
        if not active_thread:
            count = db.query(func.count(AIThread.id)).filter(AIThread.user_id == user.id).scalar() or 0
            active_thread = AIThread(user_id=user.id, title=f"Chat {count + 1}")
            db.add(active_thread)
            db.commit()
            threads.insert(0, active_thread)
        # Attach any orphaned chats to the active thread so history stays visible
        orphans = db.query(AIChat).filter(AIChat.user_id == user.id, AIChat.thread_id.is_(None)).all()
        if orphans:
            for ch in orphans:
                ch.thread_id = active_thread.id
            db.commit()
        messages = _thread_messages(db, active_thread.id, user.id, limit=200)
        serialized_threads = [_serialize_thread(t) for t in threads]
        serialized_messages = [_serialize_chat(m) for m in messages]
        session['ai_thread_id'] = active_thread.id
    finally:
        db.close()
    return render_template(
        'ai.html',
        threads=serialized_threads,
        active_thread=active_thread,
        messages=serialized_messages,
        ollama_ok=ollama_ok
    )

def _run_update_from_dump(dump_path: Path):
    """Helper to run update_from_dump subprocess and flash/log result."""
    script_path = Path(__file__).parent / 'scripts' / 'update_from_dump.py'
    target_path = Path(DATA_DIR) / LEAGUE_DATA_FILENAME
    if not script_path.exists():
        flash('Update script missing. Cannot refresh data.', 'danger')
        return
    cmd = [
        sys.executable, str(script_path),
        '--dump', str(dump_path),
        '--target', str(target_path),
        '--backup-dir', str(Path(DATA_DIR) / 'backups')
    ]
    try:
        auto_backup(LEAGUE_DATA_FILENAME)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        if result.returncode == 0:
            flash(f'League data updated from {dump_path.name}.', 'success')
            log_action(get_current_user().id, 'update_data', target=target_path.name, detail=dump_path.name)
            if stdout:
                flash(stdout.splitlines()[-1], 'info')
        else:
            flash('Data update failed. Check logs/output.', 'danger')
            if stderr:
                flash(stderr.splitlines()[-1], 'warning')
    except subprocess.TimeoutExpired:
        flash('Data update timed out.', 'danger')
    except Exception as e:
        logging.error(f'update_from_dump helper error: {e}')
        flash('Unexpected error during data update.', 'danger')

# --- About route ---
@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/resources')
def resources():
    """Rules, manuals, and reference links."""
    return render_template('resources.html')

@app.route('/equalizer')
def equalizer_page():
    """Explain APA Equalizer scoring with live examples from league data."""
    league = load_league_data()
    teams = league.get('teams', {}) or {}
    matches = list((league.get('matches') or {}).values())

    def team_name(tid: str) -> str:
        t = teams.get(tid, {})
        return pretty_team_name(tid, t.get('name', str(tid)))

    def build_examples(team_id: str, fmt_keyword: str, limit: int = 4):
        rows = []
        fmt_keyword = fmt_keyword.lower()
        for m in matches:
            fmt = (m.get('format') or '').lower()
            if fmt_keyword not in fmt:
                continue
            side = None
            if m.get('home_team_id') == team_id:
                side = 'home'
            elif m.get('away_team_id') == team_id:
                side = 'away'
            else:
                continue
            scores = m.get('team_scores') or {}
            opp_id = m.get('away_team_id') if side == 'home' else m.get('home_team_id')
            games_for = scores.get(f'{side}_subtotal') or 0
            bonus_for = scores.get(f'{side}_bonus') or 0
            total_for = scores.get(f'{side}_total') or (games_for + bonus_for)
            opp_side = 'home' if side == 'away' else 'away'
            rows.append({
                'date': m.get('date'),
                'week': m.get('week') or m.get('week_number'),
                'team': team_name(team_id),
                'opponent': team_name(opp_id),
                'home': side == 'home',
                'games_for': games_for,
                'games_against': scores.get(f'{opp_side}_subtotal') or 0,
                'bonus_for': bonus_for,
                'bonus_against': scores.get(f'{opp_side}_bonus') or 0,
                'total_for': total_for,
                'total_against': scores.get(f'{opp_side}_total') or 0,
                'handicap_flag': m.get('over_under') or m.get('overUnder') or m.get('handicap_flag') or m.get('over_under_value') or None,
            })
        rows = sorted(rows, key=lambda r: r.get('date') or '', reverse=True)
        return rows[:limit]

    examples_8 = build_examples('cue_ligans_8', '8')
    examples_9 = build_examples('cue_ligans_9', '9')

    def standings_snapshot(team_id: str):
        # pull from division standings if present
        team = teams.get(team_id, {})
        div_id = team.get('division_id')
        divisions = league.get('divisions', {}) or {}
        div = divisions.get(div_id, {})
        row = next((r for r in div.get('standings', []) if r.get('team_id') == team_id), {})
        points = row.get('points') or team.get('session_summary', {}).get('session_total_points')
        # compute simple ppm from matches
        ex = examples_8 if '8' in team_id else examples_9
        played = len(ex)
        avg_pts = round(sum(r.get('total_for', 0) or 0 for r in ex) / played, 2) if played else 0
        return {
            'team': team_name(team_id),
            'division': div.get('name') or div_id,
            'points': points,
            'played': played,
            'avg_per_match': avg_pts,
            'format': team.get('format'),
        }

    snapshots = [standings_snapshot('cue_ligans_8'), standings_snapshot('cue_ligans_9')]

    return render_template(
        'equalizer.html',
        examples_8=examples_8,
        examples_9=examples_9,
        session_label=(league.get('meta') or {}).get('session'),
        snapshots=snapshots
    )

@app.route('/locker', methods=['GET'])
def locker():
    """Team community hub (Locker) - renders selected channel and messages."""
    session_db = locker_session()
    channels = []
    active_channel = None
    messages = []
    message_counts = {}
    user = get_current_user()
    try:
        base_query = session_db.query(Channel).filter(or_(Channel.is_active == True, Channel.is_active.is_(None)))
        if not (user and is_admin(user)):
            base_query = base_query.filter(or_(Channel.is_hidden == False, Channel.is_hidden.is_(None)))
        channels = base_query.order_by(Channel.position, Channel.name).all()
        # pick active channel
        slug = (request.args.get('channel') or 'general').lower()
        active_channel = next((c for c in channels if c.slug == slug), None)
        if not active_channel and channels:
            active_channel = next((c for c in channels if c.slug == 'general'), channels[0])
        if active_channel:
            messages = (
                session_db.query(Message)
                .filter_by(channel_id=active_channel.id)
                .order_by(Message.created_at.asc())
                .limit(200)
                .all()
            )
        # optional counts
        for c in channels:
            message_counts[c.id] = session_db.query(Message).filter_by(channel_id=c.id).count()
        # add display time helper
        for m in messages:
            try:
                ts = m.created_at
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                m.display_time = ts.astimezone(timezone.utc).strftime("%b %d, %I:%M %p UTC") if ts else ''
            except Exception:
                m.display_time = ''
    finally:
        session_db.close()

    # Partial response for dynamic channel switching
    if request.headers.get('X-Requested-With') == 'fetch-locker':
        html = render_template(
            'locker_messages_fragment.html',
            messages=messages,
            active_channel=active_channel,
            message_counts=message_counts
        )
        return jsonify({
            "html": html,
            "channel": active_channel.slug if active_channel else 'general',
            "channel_name": active_channel.name if active_channel else 'Locker',
            "channel_desc": active_channel.description or "Team Chat • Highlights • Strategy • Memes"
        })

    return render_template(
        'locker.html',
        channels=channels,
        active_channel=active_channel,
        messages=messages,
        message_counts=message_counts
    )


@app.route('/locker/post', methods=['POST'])
def locker_post():
    """Handle new locker message submissions."""
    user = get_current_user()
    slug = (request.form.get('channel_slug') or 'general').lower()
    if user and (user.is_banned or user.is_suspended):
        flash('Posting is disabled for your account.', 'danger')
        return redirect(url_for('locker', channel=slug))
    if user and user.is_muted:
        flash('You are muted and cannot post right now.', 'warning')
        return redirect(url_for('locker', channel=slug))
    if not user:
        return redirect(url_for('login', next=url_for('locker', channel=slug)))
    session_db = locker_session()
    content = (request.form.get('content') or '').strip()
    author_name = user.display_name or user.username
    if len(content) > 1000:
        content = content[:1000]
    try:
        channel = session_db.query(Channel).filter_by(slug=slug).first()
        if not channel:
            channel = session_db.query(Channel).filter_by(slug='general').first()
        if not channel or not content:
            flash('Message not posted: missing channel or text.', 'warning')
            return redirect(url_for('locker', channel=slug))
        if channel.is_locked and not is_admin(user):
            flash('Channel is locked.', 'warning')
            return redirect(url_for('locker', channel=slug))
        if channel.is_hidden and not is_admin(user):
            flash('Channel is hidden.', 'warning')
            return redirect(url_for('locker'))
        if user.memes_only and channel.slug not in ['memes-media','memes','memes_media']:
            flash('You are restricted to memes/media channel.', 'warning')
            return redirect(url_for('locker', channel='memes-media'))
        msg = Message(
            channel_id=channel.id,
            author_name=author_name,
            author_initials=user.initials or initials_from_name(author_name),
            content=content,
            created_at=datetime.utcnow(),
            user_id=user.id
        )
        session_db.add(msg)
        session_db.commit()
        # Auto-award badges based on activity
        try:
            award_badges_for_message(session_db, user, channel, msg)
        except Exception as e:
            logging.warning(f'Auto-badge award failed: {e}')
        return redirect(url_for('locker', channel=channel.slug))
    except Exception as e:
        logging.error(f'Locker post error: {e}')
        flash('Could not post message.', 'danger')
        return redirect(url_for('locker', channel=slug))
    finally:
        session_db.close()

@app.route('/lab', methods=['GET', 'POST'])
def lab():
    league = load_league_data()
    divisions = league.get('divisions', {})
    teams = league.get('teams', {})
    players = league.get('players', {})
    cue8, cue9 = find_cue_team_ids(league)

    divisions_list = []
    div_formats = {}
    for div_id, div in divisions.items():
        fmt = get_format_for_division(div_id)
        div_formats[div_id] = fmt
        divisions_list.append({
            'id': div_id,
            'name': get_division_label(div_id, div),
            'format': fmt
        })
    divisions_list.sort(key=lambda d: d['name'])

    teams_by_div = {}
    team_formats = {}
    for tid, t in teams.items():
        div_id = t.get('division_id')
        fmt = get_format_for_division(div_id)
        team_formats[tid] = fmt
        teams_by_div.setdefault(div_id, []).append({
            'id': tid,
            'name': pretty_team_name(tid, t.get('name', tid)),
            'format': fmt
        })
    for lst in teams_by_div.values():
        lst.sort(key=lambda x: x['name'])

    def team_name(tid):
        return pretty_team_name(tid, teams.get(tid, {}).get('name', tid))

    card = request.form.get('card')
    predictor_result = None
    sim_result = None
    lineup_result = None
    trend_result = None
    winprob_result = None
    progression_result = None

    # --- Matchup Predictor ---
    predictor_div = None
    predictor_our = None
    predictor_opp = None

    if card == 'predictor':
        try:
            predictor_div = request.form.get('division_id') or (divisions_list[0]['id'] if divisions_list else None)
            fmt = get_format_for_division(predictor_div or '')
            default_us = cue8 if fmt == '8-ball' else cue9
            predictor_our = request.form.get('our_team') or default_us
            predictor_opp = request.form.get('opp_team')
            if not predictor_opp and predictor_div in teams_by_div:
                predictor_opp = next((t['id'] for t in teams_by_div[predictor_div] if t['id'] != predictor_our), None)
            if predictor_our and predictor_opp:
                predictor_result = predict_match(predictor_div, fmt, predictor_our, predictor_opp, league)
        except Exception as e:
            logging.error(f"Matchup predictor error: {e}")

    # --- Simulation Model ---
    if card == 'simulate':
        try:
            div_id = request.form.get('sim_division') or (divisions_list[0]['id'] if divisions_list else None)
            fmt = get_format_for_division(div_id or '')
            try:
                sims = int(request.form.get('sim_count', 200))
            except Exception:
                sims = 200
            sims = max(10, min(2000, sims))
            standings_rows = divisions.get(div_id, {}).get('standings', [])
            remaining_weeks = 4
            current_points = {row.get('team_id'): row.get('points', 0) for row in standings_rows}
            expected = {}
            for row in standings_rows:
                tid = row.get('team_id')
                metrics = compute_team_rating(tid, fmt, league)
                expected[tid] = metrics['ppm_avg'] * 10 if metrics['ppm_avg'] else 50
            top_counts = {tid: {'first': 0, 'top3': 0} for tid in current_points.keys()}
            import math
            team_list = list(current_points.keys())
            for _ in range(sims):
                totals = {}
                for tid in team_list:
                    mean = expected.get(tid, 50)
                    total = current_points.get(tid, 0)
                    for _w in range(remaining_weeks):
                        total += max(0, random.gauss(mean, mean * 0.1))
                    totals[tid] = total
                ranked = sorted(team_list, key=lambda x: totals.get(x, 0), reverse=True)
                for idx, tid in enumerate(ranked):
                    if idx == 0:
                        top_counts[tid]['first'] += 1
                    if idx < 3:
                        top_counts[tid]['top3'] += 1
            rows = []
            for tid in team_list:
                rows.append({
                    'team': team_name(tid),
                    'current': current_points.get(tid, 0),
                    'pct_first': round(100 * top_counts[tid]['first'] / sims, 1),
                    'pct_top3': round(100 * top_counts[tid]['top3'] / sims, 1),
                    'cue': normalize_team_name(team_name(tid)) == MY_TEAM_KEY
                })
            rows.sort(key=lambda r: r['pct_first'], reverse=True)
            sim_result = {'division': div_id, 'format': fmt, 'sims': sims, 'rows': rows}
        except Exception as e:
            logging.error(f"Simulation card error: {e}")

    # --- Skill Progression (by player) ---
    if card == 'progression':
        target_player = request.form.get('progression_player')
        fmt = request.form.get('progression_format', '8-ball')
        # default to logged-in user's mapped player if available
        if not target_player:
            me = get_current_user()
            if me and me.player_ref:
                target_player = me.player_ref
        # Build date -> week map from schedule.json
        date_to_week = {}
        try:
            schedule_json = load_json('schedule.json') or []
            for wk in schedule_json:
                if isinstance(wk, dict) and wk.get('date'):
                    date_to_week[wk['date']] = wk.get('week', 0)
        except Exception:
            pass
        try:
            if target_player:
                pts = []
                matches = league.get('matches', {}) or {}
                for m in matches.values():
                    if m.get('format') != fmt:
                        continue
                    date = m.get('date')
                    wk = date_to_week.get(date)
                    for s in m.get('sets', []) or []:
                        if s.get('home_player_id') == target_player:
                            pts.append((wk, date, s.get('home_skill_level')))
                        elif s.get('away_player_id') == target_player:
                            pts.append((wk, date, s.get('away_skill_level')))
                # sort by week, then date
                pts.sort(key=lambda x: (x[0] if x[0] is not None else 999, x[1] or ''))
                # collapse to latest per week
                week_sl = {}
                for wk, dt, sl in pts:
                    if wk is None:
                        continue
                    week_sl[wk] = sl
                labels = sorted(week_sl.keys())
                data = [week_sl[w] for w in labels]
                progression_result = {
                    'player_id': target_player,
                    'player_name': players.get(target_player, {}).get('full_name') or players.get(target_player, {}).get('short_name') or target_player,
                    'format': fmt,
                    'labels': labels,
                    'data': data,
                }
            else:
                progression_result = {'error': 'No player selected'}
        except Exception as e:
            logging.error(f"Progression card error: {e}")
            progression_result = {'error': 'progression_failed'}

    # --- Lineup Optimizer ---
    if card == 'lineup':
        fmt = request.form.get('lineup_format', '8-ball')
        team_id = request.form.get('lineup_team') or (cue8 if fmt == '8-ball' else cue9)
        try:
            sl_cap = int(request.form.get('sl_cap', 23))
        except Exception:
            sl_cap = 23
        team = teams.get(team_id, {})
        roster_ids = team_roster_ids(team)
        roster = []
        for pid in roster_ids:
            p = players.get(pid, {})
            if not p:
                continue
            m = player_metrics(p, fmt)
            roster.append({
                'id': pid,
                'name': p.get('full_name', pid),
                'sl': m['sl'],
                'ppm': m['ppm'],
                'pa': m['pa'],
            })
        best = []
        size = min(5, len(roster)) if roster else 0
        if size:
            for combo in itertools.combinations(roster, size):
                sl_total = sum(c['sl'] for c in combo)
                if sl_total > sl_cap:
                    continue
                ppm_avg = sum(c['ppm'] for c in combo) / len(combo)
                pa_avg = sum(c['pa'] for c in combo) / len(combo)
                score = ppm_avg + (pa_avg * 10)
                best.append({'players': combo, 'sl_total': sl_total, 'score': score, 'ppm_avg': ppm_avg, 'pa_avg': pa_avg})
            best.sort(key=lambda x: x['score'], reverse=True)
            best = best[:3]
        lineup_result = {'team': team_name(team_id), 'format': fmt, 'sl_cap': sl_cap, 'lineups': best}

    # --- Player Trends ---
    if card == 'trends':
        fmt = request.form.get('trend_format', '8-ball')
        pid = request.form.get('trend_player')
        cue_team = cue8 if fmt == '8-ball' else cue9
        cue_roster = team_roster_ids(teams.get(cue_team, {}))
        if not pid and cue_roster:
            pid = cue_roster[0]
        if not pid:
            trend_result = {'error': 'No player available'}
        else:
            player = players.get(pid) or players.get(str(pid), {})
            m = player_metrics(player, fmt)
            team_metrics = [player_metrics(players.get(rid, {}), fmt) for rid in cue_roster if players.get(rid)]
            team_ppm = sum(tm['ppm'] for tm in team_metrics) / len(team_metrics) if team_metrics else 0
            diff = m['ppm'] - team_ppm
            trend_text = 'Stable'
            if diff > 0.5:
                trend_text = 'Trending up'
            elif diff < -0.5:
                trend_text = 'Trending down'
            trend_result = {
                'player': player.get('full_name', pid),
                'format': fmt,
                'sl': m['sl'],
                'win_pct': round(m['win_pct'] * 100, 1),
                'ppm': round(m['ppm'], 2),
                'pa': round(m['pa'] * 100, 1),
                'team_ppm': round(team_ppm, 2),
                'diff': round(diff, 2),
                'trend': trend_text,
            }
        player = players.get(pid) or players.get(str(pid), {})
        m = player_metrics(player, fmt)
        # team average
        team_metrics = [player_metrics(players.get(rid, {}), fmt) for rid in cue_roster if players.get(rid)]
        team_ppm = sum(tm['ppm'] for tm in team_metrics) / len(team_metrics) if team_metrics else 0
        diff = m['ppm'] - team_ppm
        trend_text = 'Stable'
        if diff > 0.5:
            trend_text = 'Trending up'
        elif diff < -0.5:
            trend_text = 'Trending down'
        trend_result = {
            'player': player.get('full_name', pid),
            'format': fmt,
            'sl': m['sl'],
            'win_pct': round(m['win_pct'] * 100, 1),
            'ppm': round(m['ppm'], 2),
            'pa': round(m['pa'] * 100, 1),
            'team_ppm': round(team_ppm, 2),
            'diff': round(diff, 2),
            'trend': trend_text,
        }

    # --- Win Probability ---
    if card == 'winprob':
        fmt = request.form.get('win_format', '9-ball')
        our_pid = request.form.get('win_ours')
        opp_pid = request.form.get('win_theirs')
        def player_rating(pid):
            p = players.get(pid, {})
            m = player_metrics(p, fmt)
            rating = (m['sl'] * 10) + (m['ppm']) + (m['pa'] * 10)
            return rating, m, p.get('full_name', pid)
        if our_pid and opp_pid:
            r1, m1, n1 = player_rating(our_pid)
            r2, m2, n2 = player_rating(opp_pid)
            prob = r1 / (r1 + r2 + 1e-6)
            prob = max(0.1, min(0.9, prob))
            winprob_result = {
                'format': fmt,
                'our_name': n1,
                'their_name': n2,
                'prob': round(prob * 100, 1),
                'detail': f"Based on SL ({m1['sl']} vs {m2['sl']}), PPM ({m1['ppm']:.1f} vs {m2['ppm']:.1f}), PA ({m1['pa']*100:.1f}% vs {m2['pa']*100:.1f}%)."
            }

    # Build select lists
    cue_players = {'8-ball': [], '9-ball': []}
    other_players = {'8-ball': [], '9-ball': []}
    cue_roster_ids = []
    if cue8:
        ids = team_roster_ids(teams.get(cue8, {}))
        cue_roster_ids.extend(ids)
        for pid in ids:
            p = players.get(pid, {})
            if p:
                cue_players['8-ball'].append({'id': pid, 'name': p.get('full_name', pid)})
    if cue9:
        ids = team_roster_ids(teams.get(cue9, {}))
        cue_roster_ids.extend(ids)
        for pid in ids:
            p = players.get(pid, {})
            if p:
                cue_players['9-ball'].append({'id': pid, 'name': p.get('full_name', pid)})
    cue_roster_ids = set(cue_roster_ids)
    for pid, p in players.items():
        fmt_keys = (p.get('current_skill_levels') or {}).keys()
        if pid in cue_roster_ids:
            continue
        if '8-ball' in fmt_keys:
            other_players['8-ball'].append({'id': pid, 'name': p.get('full_name', pid)})
        if '9-ball' in fmt_keys:
            other_players['9-ball'].append({'id': pid, 'name': p.get('full_name', pid)})

    # Default lineup view
    default_format = '8-ball'
    default_team = cue8
    if not default_team:
        # pick first 8-ball team if available
        for div_id, tlist in teams_by_div.items():
            if get_format_for_division(div_id) == '8-ball' and tlist:
                default_team = tlist[0]['id']
                break
        if not default_team:
            # fallback to any team
            any_div = next(iter(teams_by_div.values()), [])
            default_team = any_div[0]['id'] if any_div else None
    lineup_roster = []
    if default_team:
        lineup_roster = get_roster_for_team(default_team, default_format, league)

    # Player options for progression card
    player_options = []
    for pid, p in players.items():
        player_options.append({
            'id': pid,
            'name': p.get('full_name') or p.get('short_name') or pid
        })
    player_options.sort(key=lambda x: x['name'])

    return render_template(
        'lab.html',
        divisions=divisions_list,
        teams_by_div=teams_by_div,
        predictor_result=predictor_result,
        sim_result=sim_result,
        lineup_result=lineup_result,
        trend_result=trend_result,
        winprob_result=winprob_result,
        progression_result=progression_result,
        cue8=cue8,
        cue9=cue9,
        cue_players=cue_players,
        other_players=other_players,
        predictor_div=predictor_div,
        predictor_our=predictor_our,
        predictor_opp=predictor_opp,
        lineup_roster=lineup_roster,
        lineup_default_team=default_team,
        lineup_default_format=default_format,
        team_formats=team_formats,
        div_formats=div_formats,
        player_options=player_options
    )

@app.route('/lab/predict', methods=['POST'])
def lab_predict():
    league = load_league_data()
    div_id = request.form.get('division_id', '')
    fmt = get_format_for_division(div_id)
    our_team = request.form.get('our_team')
    opp_team = request.form.get('opp_team')
    if not (our_team and opp_team):
        return {'error': 'Missing teams'}, 400
    try:
        result = predict_match(div_id, fmt, our_team, opp_team, league)
        # Adapt for frontend expectations
        return {
            'ok': True,
            'division': div_id,
            'format': fmt,
            'our': result.get('ours'),
            'opp': result.get('theirs'),
            'our_name': result.get('our_team'),
            'opp_name': result.get('opp_team'),
            'our_rating': result.get('our_rating'),
            'their_rating': result.get('their_rating'),
            'projection': f"{result.get('our_team')} {result.get('proj_us')} – {result.get('opp_team')} {result.get('proj_them')}",
            'race_edge': result.get('summary'),
            'reasons': result.get('reasons', [])
        }
    except Exception as e:
        logging.error(f'Predict error: {e}')
        return {'error': 'Unable to predict'}, 500

@app.route('/lab/progression', methods=['POST'])
def lab_progression():
    """Return skill progression JSON for a player/format to power the popup chart."""
    league = load_league_data()
    players = league.get('players', {}) or {}
    target_player = request.form.get('progression_player')
    fmt = request.form.get('progression_format', '8-ball')
    if not target_player:
        return {'error': 'No player selected'}, 400
    # Build date -> week map from schedule.json
    date_to_week = {}
    try:
        schedule_json = load_json('schedule.json') or []
        for wk in schedule_json:
            if isinstance(wk, dict) and wk.get('date'):
                date_to_week[wk['date']] = wk.get('week', 0)
    except Exception:
        pass
    pts = []
    try:
        matches = league.get('matches', {}) or {}
        for m in matches.values():
            if m.get('format') != fmt:
                continue
            date = m.get('date')
            wk = date_to_week.get(date)
            for s in m.get('sets', []) or []:
                if s.get('home_player_id') == target_player:
                    pts.append((wk, date, s.get('home_skill_level')))
                elif s.get('away_player_id') == target_player:
                    pts.append((wk, date, s.get('away_skill_level')))
        pts.sort(key=lambda x: (x[0] if x[0] is not None else 999, x[1] or ''))
        week_sl = {}
        for wk, dt, sl in pts:
            if wk is None:
                continue
            week_sl[wk] = sl
        labels = sorted(week_sl.keys())
        data = [week_sl[w] for w in labels]
        return {
            'ok': True,
            'player_id': target_player,
            'player_name': players.get(target_player, {}).get('full_name') or players.get(target_player, {}).get('short_name') or target_player,
            'format': fmt,
            'labels': labels,
            'data': data,
        }
    except Exception as e:
        logging.error(f'Progression API error: {e}')
        return {'error': 'progression_failed'}, 500

@app.route('/lab/simulate', methods=['POST'])
def lab_simulate():
    league = load_league_data()
    div_id = request.form.get('sim_division', '')
    fmt = get_format_for_division(div_id)
    try:
        sims = int(request.form.get('sim_count', 200))
    except Exception:
        sims = 200
    sims = max(10, min(2000, sims))
    try:
        remaining = int(request.form.get('sim_weeks', 4))
    except Exception:
        remaining = 4
    remaining = max(1, min(10, remaining))
    try:
        variance = float(request.form.get('sim_variance', 0.1))
    except Exception:
        variance = 0.1
    variance = max(0.02, min(0.5, variance))
    try:
        rows = run_simulation(div_id, sims, league, fmt, remaining_weeks=remaining, variance=variance)
        return {'rows': rows, 'meta': {'sims': sims, 'weeks': remaining, 'variance': variance}}
    except Exception as e:
        logging.error(f'Simulate error: {e}')
        return {'error': 'Unable to simulate'}, 500

@app.route('/lab/lineup', methods=['POST'])
def lab_lineup():
    league = load_league_data()
    fmt = request.form.get('lineup_format', '8-ball')
    team_id = request.form.get('lineup_team')
    strategy = request.form.get('strategy', 'balanced')
    opponent = request.form.get('lineup_opponent')
    try:
        sl_cap = int(request.form.get('sl_cap', 23))
    except Exception:
        sl_cap = 23
    try:
        sl_min = int(request.form.get('sl_min', 0)) if request.form.get('sl_min') else None
    except Exception:
        sl_min = None
    try:
        sl_max = int(request.form.get('sl_max', 0)) if request.form.get('sl_max') else None
    except Exception:
        sl_max = None
    try:
        min_low_sl = int(request.form.get('min_low_sl', 0))
    except Exception:
        min_low_sl = 0
    require_high_sl = request.form.get('require_high_sl') == 'on'
    includes = set(request.form.getlist('include_player'))
    locks = set(request.form.getlist('lock_player'))
    if not team_id:
        return {'error': 'Missing team'}, 400
    try:
        lineups = generate_lineup_advanced(fmt, team_id, sl_cap, league, includes=includes, locks=locks, strategy=strategy, opp_team_id=opponent, sl_min=sl_min, sl_max=sl_max, min_low_sl=min_low_sl, require_high_sl=require_high_sl)
        top = lineups[0] if lineups else None
        alternatives = lineups[1:11] if lineups else []
        return {'top': top, 'alternatives': alternatives}
    except Exception as e:
        logging.error(f'Lineup error: {e}')
        return {'error': 'Unable to build lineup'}, 500

@app.route('/lab/trend', methods=['POST'])
def lab_trend():
    league = load_league_data()
    fmt = request.form.get('trend_format', '8-ball')
    pid = request.form.get('trend_player')
    team = request.form.get('trend_team')
    range_map = {'3':3,'5':5,'session':0}
    recent_n = range_map.get(request.form.get('trend_range','5'),5)
    if not pid:
        return {'error': 'Missing player'}, 400
    try:
        trend = build_player_trend(pid, fmt, league, recent_n if recent_n else None or 0)
        return trend
    except Exception as e:
        logging.error(f'Trend error: {e}')
        return {'error': 'Unable to load trend'}, 500

@app.route('/lab/winprob', methods=['POST'])
def lab_winprob():
    league = load_league_data()
    fmt = request.form.get('win_format', '9-ball')
    our_pid = request.form.get('win_ours')
    opp_pid = request.form.get('win_theirs')
    if not (our_pid and opp_pid):
        return {'error': 'Missing players'}, 400
    try:
        result = win_probability(fmt, our_pid, opp_pid, league)
        return result
    except Exception as e:
        logging.error(f'Winprob error: {e}')
        return {'error': 'Unable to compute probability'}, 500

@app.route('/lab/sltracker', methods=['POST'])
def lab_sltracker():
    league = load_league_data()
    fmt = request.form.get('sl_format', '8-ball')
    pid = request.form.get('sl_player')
    range_map = {'3':3,'5':5,'session':0}
    recent_n = range_map.get(request.form.get('sl_range','5'),5)
    if not pid:
        return {'error': 'Missing player'}, 400
    try:
        tracker = build_sl_tracker(pid, fmt, league, recent_n if recent_n else None or 0)
        return tracker
    except Exception as e:
        logging.error(f'SL tracker error: {e}')
        return {'error': 'Unable to load SL tracker'}, 500

@app.route('/lab/powerrank', methods=['POST'])
def lab_powerrank():
    league = load_league_data()
    # Accept legacy field names and current UI names
    ranking_type = request.form.get('power_type') or request.form.get('pr_type') or 'team'
    fmt = request.form.get('power_format') or request.form.get('pr_format') or '8-ball'
    scope = request.form.get('power_scope') or request.form.get('pr_scope') or 'division'
    recent = scope == 'last3'
    try:
        if ranking_type == 'team':
            rankings = compute_team_power(fmt, league, recent_weeks=3 if recent else 5)
        else:
            team_only = None
            if scope in ('team', 'team_only'):
                cue8, cue9 = find_cue_team_ids(league)
                team_only = cue8 if fmt == '8-ball' else cue9
            rankings = compute_player_power(fmt, league, scope_team=team_only, recent_count=3 if recent else 5)
        top = rankings[0] if rankings else None
        return {'rankings': rankings, 'top': top}
    except Exception as e:
        logging.error(f'Power rank error: {e}')
        return {'error': 'Unable to compute rankings'}, 500

@app.route('/lab/momentum', methods=['POST'])
def lab_momentum():
    league = load_league_data()
    teams = league.get('teams', {}) or {}
    scope = request.form.get('momentum_scope', 'team')
    fmt = request.form.get('momentum_format', '8-ball')
    rng = request.form.get('momentum_range', '3')

    def parse_date(d):
        from datetime import datetime
        try:
            return datetime.fromisoformat(d)
        except Exception:
            return None

    def recent_filter(items, count=None, weeks=None):
        # items should be sorted by date ascending
        if weeks:
            # collect last N distinct dates
            dates = []
            for it in reversed(items):
                dt = parse_date(it.get('date', '')) or it.get('date')
                if dt not in dates:
                    dates.append(dt)
                it['__keep'] = len(dates) <= weeks
            return [it for it in items if it.get('__keep')]
        if count:
            return items[-count:] if items else []
        return items

    def team_momentum(team_id):
        matches = []
        for m in league.get('matches', {}).values():
            if m.get('format') not in (fmt, fmt.replace('_', '-')):
                continue
            if m.get('home_team_id') == team_id or m.get('away_team_id') == team_id:
                entry = {
                    'date': m.get('date'),
                    'opp': m.get('away_team_id') if m.get('home_team_id') == team_id else m.get('home_team_id'),
                    'points_for': m.get('team_scores', {}).get('home_total' if m.get('home_team_id') == team_id else 'away_total', 0) or 0,
                    'points_against': m.get('team_scores', {}).get('away_total' if m.get('home_team_id') == team_id else 'home_total', 0) or 0,
                }
                entry['win'] = entry['points_for'] > entry['points_against']
                matches.append(entry)
        matches.sort(key=lambda x: parse_date(x.get('date','')) or x.get('date',''))

        if rng == '3':
            recent = recent_filter(matches, count=3)
        elif rng == '5':
            recent = recent_filter(matches, count=5)
        elif rng == '3w':
            recent = recent_filter(matches, weeks=3)
        else:
            recent = matches

        def agg(ms):
            if not ms:
                return {'ppm': 0, 'win_pct': 0, 'margin': 0}
            ppm = sum(m['points_for'] for m in ms)/len(ms)
            win_pct = sum(1 for m in ms if m['win'])/len(ms)
            margin = sum(m['points_for']-m['points_against'] for m in ms)/len(ms)
            return {'ppm': ppm, 'win_pct': win_pct, 'margin': margin}

        full = agg(matches)
        recent_stats = agg(recent)
        delta_ppm = recent_stats['ppm'] - full['ppm']
        delta_win = recent_stats['win_pct'] - full['win_pct']
        delta_margin = recent_stats['margin'] - full['margin']
        score = (delta_ppm * 1.2) + (delta_win * 50) + (delta_margin * 0.4)
        if score > 6:
            label = 'Surging'
        elif score > 2:
            label = 'Warming'
        elif score < -6:
            label = 'Slumping'
        elif score < -2:
            label = 'Cooling'
        else:
            label = 'Stable'

        swings = sorted(recent, key=lambda m: abs((m['points_for']-m['points_against'])), reverse=True)[:3]
        return {
            'team': pretty_team_name(team_id, team_id),
            'format': fmt,
            'label': label,
            'recent': recent_stats,
            'season': full,
            'delta_ppm': delta_ppm,
            'delta_win': delta_win,
            'recent_matches': [
                {
                    'date': m.get('date'),
                    'opp': pretty_team_name(m.get('opp'), teams.get(m.get('opp'), {}).get('name', m.get('opp'))),
                    'pf': m['points_for'],
                    'pa': m['points_against'],
                    'win': m['win'],
                    'margin': m['points_for'] - m['points_against']
                } for m in recent
            ],
            'swings': [
                {
                    'date': m.get('date'),
                    'opp': pretty_team_name(m.get('opp'), m.get('opp')),
                    'margin': m['points_for'] - m['points_against'],
                    'pf': m['points_for'],
                    'pa': m['points_against']
                } for m in swings
            ]
        }

    def player_momentum(team_id):
        roster = team_roster_ids(league.get('teams', {}).get(team_id, {}))
        players = league.get('players', {}) or {}
        result = []
        for pid in roster:
            p = players.get(pid, {})
            base = player_metrics(p, fmt)
            recs = player_matches(pid, fmt, league)
            recs.sort(key=lambda r: parse_date(r.get('date','')) or r.get('date',''))
            if rng == '3':
                recent = recs[-3:]
            elif rng == '5':
                recent = recs[-5:]
            elif rng == '3w':
                recent = recs[-6:]  # approx two per week
            else:
                recent = recs
            def avg(lst, key):
                return sum(x.get(key,0) for x in lst)/len(lst) if lst else 0
            recent_ppm = avg(recent, 'points_for')
            recent_win = sum(1 for r in recent if r.get('outcome')=='W')/len(recent) if recent else 0
            delta_ppm = recent_ppm - base['ppm']
            delta_win = recent_win - base['win_pct']
            score = (delta_ppm * 1.5) + (delta_win * 60)
            if score > 8:
                label = 'Hot'
            elif score > 2:
                label = 'Warming Up'
            elif score < -8:
                label = 'Cold'
            elif score < -2:
                label = 'Cooling Off'
            else:
                label = 'Stable'
            result.append({
                'id': pid,
                'name': p.get('full_name', pid),
                'sl': base['sl'],
                'recent_ppm': recent_ppm,
                'season_ppm': base['ppm'],
                'delta_ppm': delta_ppm,
                'recent_win': recent_win,
                'season_win': base['win_pct'],
                'label': label,
                'score': score,
                'recent_matches': [{
                    'date': r.get('date'),
                    'opp_sl': r.get('opp_sl'),
                    'pts': r.get('points_for'),
                    'outcome': r.get('outcome')
                } for r in recent[-3:]]
            })
        result.sort(key=lambda r: r['score'], reverse=True)
        hot = [r for r in result if r['score'] > 4][:3]
        cold = [r for r in result if r['score'] < -4][:3]
        return {'players': result, 'hot': hot, 'cold': cold}

    cue8, cue9 = find_cue_team_ids(league)
    team_id = cue8 if fmt == '8-ball' else cue9
    if scope == 'team':
        try:
            data = team_momentum(team_id)
            data['scope'] = 'team'
            return data
        except Exception as e:
            logging.error(f'Momentum team error: {e}')
            return {'error': 'Unable to compute team momentum'}, 500
    else:
        try:
            data = player_momentum(team_id)
            data['scope'] = 'player'
            return data
        except Exception as e:
            logging.error(f'Momentum player error: {e}')
            return {'error': 'Unable to compute player momentum'}, 500

@app.route('/lab/racecalc', methods=['POST'])
def lab_racecalc():
    league = load_league_data()
    fmt = request.form.get('race_format', '8-ball')
    pid_a = request.form.get('race_a')
    pid_b = request.form.get('race_b')
    if not (pid_a and pid_b):
        return {'error': 'Missing players'}, 400
    try:
        result = race_calculation(fmt, pid_a, pid_b, league)
        return result
    except Exception as e:
        logging.error(f'Race calc error: {e}')
        return {'error': 'Unable to calculate race'}, 500

@app.route('/lab/oppscout', methods=['POST'])
def lab_oppscout():
    league = load_league_data()
    fmt = request.form.get('opp_format', '8-ball')
    opp_team = request.form.get('opp_team')
    cue8, cue9 = find_cue_team_ids(league)
    our_team = cue8 if fmt == '8-ball' else cue9
    if not opp_team:
        return {'error': 'Missing opponent'}, 400
    try:
        report = compute_opponent_scout(fmt, opp_team, league, our_team)
        return report
    except Exception as e:
        logging.error(f'Opponent scout error: {e}')
        return {'error': 'Unable to build report'}, 500

@app.route('/lab/projection', methods=['POST'])
def lab_projection():
    league = load_league_data()
    fmt = request.form.get('proj_format', '9-ball')
    opp_team = request.form.get('proj_opp')
    mode = request.form.get('proj_lineup_mode', 'best')
    includes = set(request.form.getlist('proj_player'))

    cue8, cue9 = find_cue_team_ids(league)
    our_team = cue8 if fmt == '8-ball' else cue9
    if not opp_team:
        return {'error': 'Missing opponent'}, 400

    # Base roster and lineup selection
    roster = get_roster_for_team(our_team, fmt, league)
    lineup_players = []
    if mode == 'custom' and includes:
        lineup_players = [p for p in roster if p['id'] in includes][:5]
    else:
        lineups = generate_lineup_advanced(fmt, our_team, 23, league, strategy='balanced')
        if lineups:
            lineup_players = lineups[0]['players']
        else:
            lineup_players = roster[:5]

    # Opponent defensive profile
    opp_roster = get_roster_for_team(opp_team, fmt, league)
    opp_pa_avg = sum(p.get('pa', 0) for p in opp_roster) / len(opp_roster) if opp_roster else 0.5
    opp_ppm_avg = sum(p.get('ppm', 0) for p in opp_roster) / len(opp_roster) if opp_roster else 10

    # Lineup aggregates
    lineup_ppm = sum(p.get('ppm', 0) for p in lineup_players)
    lineup_pa = sum(p.get('pa', 0) for p in lineup_players) / len(lineup_players) if lineup_players else 0.5

    # Recent momentum adjustment: compare last 3 matches to season average
    def team_recent(team_id):
        matches = []
        for m in league.get('matches', {}).values():
            if m.get('format') not in (fmt, fmt.replace('_', '-')):
                continue
            if team_id in (m.get('home_team_id'), m.get('away_team_id')):
                pf = m.get('team_scores', {}).get('home_total' if m.get('home_team_id') == team_id else 'away_total', 0) or 0
                matches.append(pf)
        matches.sort()
        return matches[-3:]

    our_recent = team_recent(our_team)
    opp_recent = team_recent(opp_team)

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    recent_boost = avg(our_recent) - avg(opp_recent)

    # Projection core: start from lineup PPM, adjust by opponent defense and momentum
    base = lineup_ppm
    defense_adj = (1 - opp_pa_avg) * 5  # tougher defense lowers projection
    momentum_adj = recent_boost * 0.1
    center = base - defense_adj + momentum_adj
    variance = max(4, min(14, abs(defense_adj) + 6))
    low = max(0, center - variance)
    high = center + variance

    # Historical vs opponent
    hist_scores = []
    for m in league.get('matches', {}).values():
        if m.get('format') not in (fmt, fmt.replace('_', '-')):
            continue
        home = m.get('home_team_id')
        away = m.get('away_team_id')
        if {home, away} == {our_team, opp_team}:
            pf = m.get('team_scores', {}).get('home_total' if home == our_team else 'away_total', 0) or 0
            hist_scores.append(pf)
    hist_avg = avg(hist_scores)
    hist_hi = max(hist_scores) if hist_scores else None
    hist_lo = min(hist_scores) if hist_scores else None

    # Difficulty rating using power scores
    def team_power(tid):
        rating = team_rating_enhanced(tid, fmt, league)
        return rating.get('rating') if isinstance(rating, dict) else 50
    our_power = team_power(our_team)
    opp_power = team_power(opp_team)
    diff = our_power - opp_power
    if diff > 8:
        difficulty = 'Very Favorable'
    elif diff > 3:
        difficulty = 'Favorable'
    elif diff > -3:
        difficulty = 'Balanced'
    elif diff > -8:
        difficulty = 'Tough'
    else:
        difficulty = 'Very Tough'

    factors = [
        f"Lineup PPM total {lineup_ppm:.1f}",
        f"Opponent defense PA% {opp_pa_avg*100:.1f}%",
        f"Momentum delta (recent pts) {recent_boost:.1f}",
        f"Power diff {diff:.1f} ({difficulty})"
    ]
    if hist_scores:
        factors.append(f"History vs opponent avg {hist_avg:.1f}")

    # Scenario quick view
    scenarios = []
    if len(lineup_players) >= 5:
        scenarios.append({'label': 'Strongest lineup', 'range': [round(center-variance,1), round(center+variance,1)]})
    if roster:
        conservative = roster[-5:]
        cons_ppm = sum(p.get('ppm', 0) for p in conservative)
        cons_center = cons_ppm - defense_adj + momentum_adj
        scenarios.append({'label': 'Conservative lineup', 'range': [round(max(0,cons_center-variance),1), round(cons_center+variance,1)]})

    return {
        'format': fmt,
        'opponent': pretty_team_name(opp_team, opp_team),
        'projection': {
            'center': round(center,1),
            'low': round(low,1),
            'high': round(high,1),
            'difficulty': difficulty
        },
        'lineup': lineup_players,
        'history': {
            'avg': round(hist_avg,1) if hist_scores else None,
            'high': hist_hi,
            'low': hist_lo,
            'count': len(hist_scores)
        },
        'factors': factors,
        'scenarios': scenarios
    }

@app.route('/lab/vegas', methods=['POST'])
def lab_vegas():
    league = load_league_data()
    fmt = request.form.get('vegas_format', '9-ball')
    target = request.form.get('vegas_target', 'top3')
    try:
        remaining_weeks = int(request.form.get('vegas_weeks', 4))
    except Exception:
        remaining_weeks = 4
    remaining_weeks = max(1, min(10, remaining_weeks))

    cue8, cue9 = find_cue_team_ids(league)
    our_team = cue8 if fmt == '8-ball' else cue9
    divisions = league.get('divisions', {})
    div_id = 'thursday_brownsville_8' if fmt == '8-ball' else 'thursday_brownsville_9'
    standings = divisions.get(div_id, {}).get('standings', [])
    standings_sorted = sorted(standings, key=lambda r: r.get('rank', 999))

    # Current status
    curr_points = 0
    curr_rank = None
    for row in standings_sorted:
        if row.get('team_id') == our_team:
            curr_points = row.get('points', 0) or 0
            curr_rank = row.get('rank')
            break
    leader_points = standings_sorted[0].get('points', 0) if standings_sorted else 0
    third_points = standings_sorted[2].get('points', 0) if len(standings_sorted) >= 3 else leader_points

    # Target thresholds
    if target == 'top3' or target == 'tricup':
        target_points = max(third_points, curr_points) + 15
        target_label = 'Top 3 / Tri-Cup'
    elif target == 'cities':
        target_points = leader_points + 10
        target_label = 'Cities / LTC'
    else:  # vegas-level
        target_points = leader_points + 20
        target_label = 'Vegas-Level Finish'

    points_needed = max(0, target_points - curr_points)
    req_avg = points_needed / remaining_weeks if remaining_weeks else points_needed

    # Scenario bands based on team scoring history
    def team_scores(team_id):
        pts = []
        for m in league.get('matches', {}).values():
            if m.get('format') not in (fmt, fmt.replace('_','-')):
                continue
            if team_id in (m.get('home_team_id'), m.get('away_team_id')):
                pf = m.get('team_scores', {}).get('home_total' if m.get('home_team_id') == team_id else 'away_total', 0) or 0
                pts.append(pf)
        return pts

    our_scores = team_scores(our_team)
    avg_score = sum(our_scores)/len(our_scores) if our_scores else 50
    conservative = max(0, avg_score * 0.9 - 3)
    expected = avg_score
    aggressive = avg_score * 1.1 + 3
    def scenario(label, per_week):
        total = curr_points + per_week * remaining_weeks
        if total >= target_points + 5:
            status = 'Solidly qualifies'
        elif total >= target_points:
            status = 'On the bubble'
        else:
            status = 'Below target'
        return {'label': label, 'per_week': round(per_week,1), 'final': round(total,1), 'status': status}
    scenarios = [
        scenario('Conservative', conservative),
        scenario('Expected', expected),
        scenario('Aggressive', aggressive)
    ]

    # Rival sensitivity (teams around us)
    rivals = []
    if curr_rank:
        for row in standings_sorted:
            r = row.get('rank')
            if abs(r - curr_rank) <= 2 and row.get('team_id') != our_team:
                rivals.append({
                    'name': pretty_team_name(row.get('team_id'), row.get('team_id')),
                    'rank': r,
                    'points': row.get('points', 0)
                })

    # Qualification probability using simulation
    prob_top3 = None
    prob_first = None
    try:
        sims = run_simulation(div_id, 200, league, fmt, remaining_weeks=remaining_weeks, variance=0.12)
        row = next((r for r in sims if normalize_team_name(r['team']) == normalize_team_name(pretty_team_name(our_team, our_team))), None)
        if row:
            prob_top3 = row.get('pct_top3')
            prob_first = row.get('pct_first')
    except Exception as e:
        logging.error(f'Vegas simulation error: {e}')

    summary = f"We need about {req_avg:.1f} points/week over {remaining_weeks} weeks to reach {target_label}."
    return {
        'format': fmt,
        'target_label': target_label,
        'current': {
            'rank': curr_rank,
            'points': curr_points,
            'gap_first': leader_points - curr_points,
            'gap_third': third_points - curr_points
        },
        'targets': {
            'target_points': round(target_points,1),
            'points_needed': round(points_needed,1),
            'required_avg': round(req_avg,2)
        },
        'scenarios': scenarios,
        'rivals': rivals,
        'prob_top3': prob_top3,
        'prob_first': prob_first,
        'summary': summary
    }

@app.route('/lab/roster', methods=['POST'])
def lab_roster():
    league = load_league_data()
    fmt = request.form.get('format', '8-ball')
    team_id = request.form.get('team_id')
    if not team_id:
        return {'error': 'Missing team'}, 400
    try:
        roster = get_roster_for_team(team_id, fmt, league)
        return {'roster': roster}
    except Exception as e:
        logging.error(f'Roster error: {e}')
        return {'error': 'Unable to load roster'}, 500

# Bonus: Trap HTTP exceptions for better debug info
app.config['TRAP_HTTP_EXCEPTIONS'] = True

# Setup logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s')

# --- Schedule route ---
@app.route('/schedule')
def schedule():
    from datetime import datetime, date as dt_date
    import pytz
    tz = pytz.timezone('America/Chicago')

    def build_schedule_with_results():
        try:
            weeks = load_json('schedule.json') or []
        except Exception:
            weeks = []
        league = load_league_data()
        teams = league.get('teams', {})
        matches = league.get('matches', {})
        players = league.get('players', {})

        def team_label(team_id):
            t = teams.get(team_id) or teams.get(str(team_id), {})
            return pretty_team_name(team_id, t.get('name', str(team_id)))

        def player_name(pid):
            p = players.get(pid) or players.get(str(pid), {})
            return p.get('full_name') or p.get('short_name') or str(pid)

        # If no schedule.json, synthesize weeks from league matches
        if not weeks and matches:
            weeks_map = {}
            for m in matches.values():
                wk = m.get('week') or 0
                date_str = m.get('date')
                rec = weeks_map.setdefault(wk, {'week': wk, 'date': date_str, 'matches': []})
                rec['matches'].append({
                    'format': m.get('format'),
                    'home': team_label(m.get('home_team_id')),
                    'away': team_label(m.get('away_team_id')),
                    'note': m.get('note')
                })
            weeks = [v for v in weeks_map.values()]
            weeks.sort(key=lambda x: x.get('week') or 0)

        # Bucket league matches by date for quick lookup
        matches_by_date = {}
        for m in matches.values():
            try:
                mid = m.get('match_id') or m.get('id')
                fmt = m.get('format', '')
                if fmt not in ('8-ball', '9-ball'):
                    continue
                date_str = m.get('date')
                if not date_str:
                    continue
                # Render sets for popup
                sets_out = []
                for s in m.get('sets', []) or []:
                    if not isinstance(s, dict):
                        continue
                    sets_out.append({
                        'order': s.get('set_order') or s.get('board'),
                        'home_player': player_name(s.get('home_player_id')),
                        'away_player': player_name(s.get('away_player_id')),
                        'home_sl': s.get('home_skill_level'),
                        'away_sl': s.get('away_skill_level'),
                        'home_points': s.get('home_points'),
                        'away_points': s.get('away_points'),
                        'winner': s.get('winner')
                    })
                entry = {
                    'match_id': mid,
                    'date': date_str,
                    'format': fmt,
                    'home': team_label(m.get('home_team_id')),
                    'away': team_label(m.get('away_team_id')),
                    'home_total': m.get('team_scores', {}).get('home_total'),
                    'away_total': m.get('team_scores', {}).get('away_total'),
                    'top_scorers': [],
                    'sets': sets_out,
                    'venue': m.get('location'),
                }
                # Extract top scorers from individual sets (points scored in that set)
                perf = []
                for s in m.get('sets', []) or []:
                    try:
                        perf.append({'player': player_name(s.get('home_player_id')), 'team': entry['home'], 'points': s.get('home_points', 0)})
                        perf.append({'player': player_name(s.get('away_player_id')), 'team': entry['away'], 'points': s.get('away_points', 0)})
                    except Exception:
                        continue
                perf = [p for p in perf if isinstance(p.get('points'), (int, float))]
                perf.sort(key=lambda x: x['points'], reverse=True)
                entry['top_scorers'] = perf[:3]
                matches_by_date.setdefault(date_str, []).append(entry)
            except Exception:
                continue

        today = datetime.now(tz).date()
        enriched = []
        for w in weeks:
            date_str = w.get('date', '')
            note = w.get('note')
            match_list = w.get('matches', []) or []
            try:
                parsed_date = dt_date.fromisoformat(date_str)
                status = 'played' if parsed_date < today else 'today' if parsed_date == today else 'upcoming'
            except Exception:
                status = 'upcoming'
            # If we have no league results, synthesize basic entries from the schedule itself
            results = matches_by_date.get(date_str, [])
            if not results and status == 'played' and match_list:
                synth = []
                for m in match_list:
                    synth.append({
                        'format': '8 & 9',
                        'home': m.get('home') or '',
                        'away': m.get('away') or '',
                        'home_total': None,
                        'away_total': None,
                        'top_scorers': []
                    })
                results = synth
            enriched.append({
                'week': w.get('week'),
                'date': date_str,
                'note': note,
                'matches': match_list,
                'status': status,
                'results': results,
            })
        return enriched

    schedule_data = build_schedule_with_results()
    return render_template('schedule.html', schedule=schedule_data)

# --- Dues tracker route ---
@app.route('/dues', methods=['GET', 'POST'])
def dues():
    dues_data = load_json('dues.json') or {}

    # Collect player list from dues data + roster fallback
    player_set = set()
    if isinstance(dues_data.get('players'), dict):
        player_set.update(dues_data['players'].keys())
    player_formats = {}
    try:
        roster = get_rosters_from_league()
        for team in roster:
            if normalize_team_name(team.get('team', '')) != MY_TEAM_KEY:
                continue
            for p in team.get('eight_ball', []):
                if p.get('name'):
                    name = p['name']
                    player_set.add(name)
                    player_formats.setdefault(name, set()).add('eight')
            for p in team.get('nine_ball', []):
                if p.get('name'):
                    name = p['name']
                    player_set.add(name)
                    player_formats.setdefault(name, set()).add('nine')
    except Exception:
        pass
    all_players = sorted(player_set)

    # Build week list from schedule (future+past)
    try:
        schedule_weeks = load_json('schedule.json') or []
    except Exception:
        schedule_weeks = []
    # Only keep weeks we actually play (have matches) and are not marked no-play/bye
    schedule_weeks = [
        w for w in schedule_weeks
        if isinstance(w, dict)
        and w.get('matches')
        and not str(w.get('note', '')).lower().startswith('no play')
    ]
    # Deduplicate and sort by week number
    seen_weeks = set()
    deduped = []
    for w in sorted(schedule_weeks, key=lambda x: x.get('week', 0)):
        wk = w.get('week')
        if wk in seen_weeks:
            continue
        seen_weeks.add(wk)
        deduped.append(w)
    schedule_weeks = deduped

    # Migrate legacy format (players paid8/paid9) into first week if needed
    if not dues_data.get('weeks'):
        legacy_players = dues_data.get('players', {})
        if legacy_players:
            status_map = {}
            for name, pd in legacy_players.items():
                status_map[name] = {
                    'eight': 'paid' if pd.get('paid8') else 'unpaid',
                    'nine': 'paid' if pd.get('paid9') else 'unpaid',
                }
            dues_data['weeks'] = [{
                'week': schedule_weeks[0].get('week') if schedule_weeks else 1,
                'date': schedule_weeks[0].get('date') if schedule_weeks else '',
                'status': status_map,
            }]

    weeks_from_file = dues_data.get('weeks', [])
    week_status_map = {}
    for w in weeks_from_file:
        if isinstance(w, dict) and 'week' in w:
            week_status_map[w['week']] = w

    weeks_combined = []
    for w in schedule_weeks:
        wk = w.get('week')
        base = week_status_map.get(wk, {})
        weeks_combined.append({
            'week': wk,
            'date': w.get('date', ''),
            'note': w.get('note', ''),
            'status': base.get('status', {}),
        })

    # Ensure every week has a status map with defaults
    def default_status_for_player(name):
        fmts = player_formats.get(name, {'eight', 'nine'})
        return {
            'eight': 'dnp' if 'eight' not in fmts else 'unpaid',
            'nine': 'dnp' if 'nine' not in fmts else 'unpaid',
        }
    for w in weeks_combined:
        st = w.setdefault('status', {})
        for p in all_players:
            st.setdefault(p, default_status_for_player(p))

    # Default selection: latest week with any status, else last week
    def has_updates(entry):
        return any(v for v in entry.get('status', {}).values())
    updated_weeks = [w for w in weeks_combined if has_updates(w)]
    default_week = (updated_weeks[-1]['week'] if updated_weeks else (weeks_combined[-1]['week'] if weeks_combined else 1))

    selected_week_num = request.args.get('week', default_week)
    try:
        selected_week_num = int(selected_week_num)
    except Exception:
        selected_week_num = default_week

    # Handle POST toggles
    if request.method == 'POST':
        player = request.form.get('player', '').strip()
        game_type = request.form.get('game_type', '')
        status_val = request.form.get('status', '')
        wk = request.form.get('week') or selected_week_num
        try:
            wk = int(wk)
        except Exception:
            wk = selected_week_num
        if player and game_type in ('eight', 'nine') and status_val in ('paid', 'unpaid', 'dnp'):
            # ensure week exists
            target_week = next((w for w in weeks_combined if w['week'] == wk), None)
            if not target_week:
                target_week = {'week': wk, 'date': '', 'note': '', 'status': {}}
                weeks_combined.append(target_week)
            status_map = target_week.setdefault('status', {})
            entry = status_map.setdefault(player, {'eight': 'unpaid', 'nine': 'unpaid'})
            entry[game_type] = status_val
            # persist
            dues_data['weeks'] = weeks_combined
            try:
                save_json('dues.json', dues_data)
            except Exception as e:
                logging.error(f"Error saving dues update: {e}")
        # redirect to selected week view
        return redirect(url_for('dues', week=wk))

    selected_week = next((w for w in weeks_combined if w['week'] == selected_week_num), None)
    if not selected_week and weeks_combined:
        selected_week = weeks_combined[-1]
        selected_week_num = selected_week['week']
    if not selected_week:
        selected_week = {'week': selected_week_num, 'status': {}, 'date': '', 'note': ''}

    status_for_week = selected_week.get('status', {})
    # Ensure all players present with defaults for selected week
    for p in all_players:
        status_for_week.setdefault(p, default_status_for_player(p))

    weekly_target = 90
    player_rows = []
    for name in all_players:
        fmts = player_formats.get(name, {'eight', 'nine'})
        st = status_for_week.get(name, {'eight': 'unpaid', 'nine': 'unpaid'})
        def amt(fmt_key):
            s = st.get(fmt_key)
            if fmt_key not in fmts:
                return 0
            if s == 'paid':
                return 9
            return 0
        owes = (0 if st.get('eight') in ('paid','dnp') or 'eight' not in fmts else 9) + (0 if st.get('nine') in ('paid','dnp') or 'nine' not in fmts else 9)
        player_rows.append({
            'name': name,
            'eight': st.get('eight', 'unpaid'),
            'nine': st.get('nine', 'unpaid'),
            'owes': owes,
            'formats': fmts,
        })

    # Totals across all play weeks
    total_target = weekly_target * max(len(weeks_combined), 1)
    collected_total = 0
    for w in weeks_combined:
        status_map = w.get('status', {})
        total_for_week = 0
        for name in all_players:
            fmts = player_formats.get(name, {'eight', 'nine'})
            st = status_map.get(name, default_status_for_player(name))
            if 'eight' in fmts and st.get('eight') == 'paid':
                total_for_week += 9
            if 'nine' in fmts and st.get('nine') == 'paid':
                total_for_week += 9
        collected_total += min(total_for_week, weekly_target)
    outstanding_total = max(total_target - collected_total, 0)
    progress_pct = int((collected_total / total_target) * 100) if total_target else 0

    return render_template(
        'dues.html',
        player_dues=player_rows,
        collected=collected_total,
        outstanding=outstanding_total,
        total_target=total_target,
        progress_pct=progress_pct,
        all_players=all_players,
        weeks=weeks_combined,
        selected_week=selected_week,
        weeks_count=len(weeks_combined),
        player_formats=player_formats,
    )

# --- Division Roster route ---
@app.route('/division_roster')
def division_roster():
    try:
        all_teams = get_rosters_from_league()
    except Exception:
        all_teams = []
    # Prepare each team's 8-ball and 9-ball rosters for display
    def prep_roster(roster):
        for p in roster:
            p['sl'] = p.get('skill_level') or 0
            try:
                raw_win = p.get('win_pct', 0)
                # normalize string like "14.29%" or float
                win_pct_val = float(str(raw_win).replace('%','')) if raw_win is not None else 0.0
                # if already a percent string like "14.29%" keep same, else if 0-1 assume ratio
                if win_pct_val <= 1 and raw_win not in (None, ''):
                    win_pct_val = win_pct_val * 100
                matches = int(str(p.get('matches','0/0')).split('/')[1])
                p['won'] = int(win_pct_val * matches / 100) if matches else 0
            except Exception:
                p['won'] = 0
            try:
                p['win_pct'] = float(str(p.get('win_pct', '0')).replace('%',''))
                if p['win_pct'] <= 1 and p.get('win_pct') not in (None, ''):
                    p['win_pct'] = p['win_pct'] * 100
            except Exception:
                p['win_pct'] = 0.0
            try:
                p['ppm'] = float(p.get('ppm', 0))
            except Exception:
                p['ppm'] = 0.0
            try:
                p['pa'] = float(str(p.get('pa', '0')).replace('%',''))
            except Exception:
                p['pa'] = 0.0
        return roster
    for team in all_teams:
        team['eight_ball'] = prep_roster(team.get('eight_ball', []))
        team['nine_ball'] = prep_roster(team.get('nine_ball', []))
    return render_template('division_roster.html', all_teams=all_teams)


# Helper to load JSON data safely
def load_json(filename):
    """Robust JSON loader that trims BOM/leading noise (e.g., stray digits)."""
    path = os.path.join(DATA_DIR, filename)
    try:
        raw = Path(path).read_text(encoding='utf-8')
        try:
            return json.loads(raw)
        except Exception:
            # Strip BOM/whitespace/digits before the first JSON bracket
            for ch in ('{', '['):
                idx = raw.find(ch)
                if idx != -1:
                    try:
                        return json.loads(raw[idx:])
                    except Exception:
                        pass
            raise
    except Exception as e:
        logging.error(f"Error loading {filename}: {e}")
        return {} if filename.endswith('.json') else []

# Patch index loader
def load_patch_index():
    data = load_json('apa_patch_index.json')
    if not isinstance(data, dict):
        return {"categories": [], "difficulty_scale": {}}
    return data

def load_planned_lineups():
    data = load_json('planned_lineups.json')
    return data if isinstance(data, dict) else {}

def save_planned_lineups(data: dict):
    try:
        Path(DATA_DIR, 'planned_lineups.json').write_text(json.dumps(data, indent=2))
    except Exception as e:
        logging.warning(f"Could not save planned_lineups.json: {e}")

# Patch index loader
def load_patch_index():
    data = load_json('apa_patch_index.json')
    if not isinstance(data, dict):
        return {"categories": [], "difficulty_scale": {}}
    return data

# Helper to save JSON data safely (thread-safe)
def save_json(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with LOCK:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

# Helper to auto-backup JSON files
def auto_backup(filename):
    import shutil
    backup_dir = os.path.join(DATA_DIR, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    src = os.path.join(DATA_DIR, filename)
    dst = os.path.join(backup_dir, f"{filename}.bak")
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        logging.warning(f"Backup failed for {filename}: {e}")

def find_latest_backup(target_filename=LEAGUE_DATA_FILENAME):
    """Find the most recent backup file for a given target filename."""
    backup_dir = Path(DATA_DIR) / 'backups'
    if not backup_dir.exists():
        return None
    pattern = f"{Path(target_filename).stem}_*.bak.json"
    candidates = list(backup_dir.glob(pattern))
    if not candidates:
        # also consider generic .bak without timestamp
        candidates = list(backup_dir.glob(f"{Path(target_filename).name}.bak"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

def run_script_with_output(label: str, cmd, cwd: Path | None = None, timeout: int = 300) -> bool:
    """Run a subprocess, flash status, and log basic output."""
    try:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        if result.returncode == 0:
            flash(f'{label} completed.', 'success')
            if stdout:
                flash(f'{label} output: {stdout.splitlines()[-1][:500]}', 'info')
            return True
        flash(f'{label} failed (exit {result.returncode}).', 'danger')
        if stderr:
            flash(f'{label} stderr: {stderr.splitlines()[-1][:500]}', 'warning')
        return False
    except subprocess.TimeoutExpired:
        flash(f'{label} timed out.', 'danger')
    except Exception as e:
        logging.error(f'{label} error: {e}')
        flash(f'{label} error: {e}', 'danger')
    return False

def run_script_with_output_collect(label: str, cmd, cwd: Path | None = None, timeout: int = 300):
    """Run a subprocess and return (ok, logs:list)."""
    logs = []
    try:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()
        if result.returncode == 0:
            logs.append(f'{label}: completed')
            if stdout:
                logs.append(f'{label} stdout: {stdout.splitlines()[-1][:500]}')
            return True, logs
        logs.append(f'{label}: failed (exit {result.returncode})')
        if stderr:
            logs.append(f'{label} stderr: {stderr.splitlines()[-1][:500]}')
        return False, logs
    except subprocess.TimeoutExpired:
        logs.append(f'{label}: timed out')
    except Exception as e:
        logs.append(f'{label}: error {e}')
    return False, logs

def build_ai_context(current_user=None):
    """Create a compact context string from league data for AI Q&A."""
    data = load_league_data()
    players = list((data.get('players') or {}).values())
    teams = list((data.get('teams') or {}).values())
    my_team = next((t for t in teams if normalize_team_name(t.get('team_name')) == MY_TEAM_KEY), None)

    def metric(p):
        for key in ('win_pct', 'percent_points_avail', 'pa', 'ppm', 'points_per_match'):
            val = p.get(key)
            if isinstance(val, (int, float)):
                return val
            try:
                return float(str(val).replace('%',''))
            except Exception:
                continue
        return 0

    top_players = sorted(players, key=metric, reverse=True)[:8]
    lines = []
    if my_team:
        lines.append(f"Team: {my_team.get('team_name')} | division: {my_team.get('division_name')} | rank: {my_team.get('rank') or '?'} | points: {my_team.get('points') or '?'}")
    if current_user and getattr(current_user, 'player_ref', None):
        slug = normalize_team_name(current_user.player_ref)
        found = next((p for p in players if normalize_team_name(p.get('full_name') or p.get('short_name') or '') == slug), None)
        if found:
            lines.append(f"User player: {found.get('full_name') or found.get('short_name')} | SL: {found.get('sl') or found.get('skill_level')} | win_pct: {found.get('win_pct') or found.get('pa')} | ppm: {found.get('ppm') or found.get('points_per_match')}")
    if top_players:
        lines.append("Top players (by win/ppm):")
        for p in top_players:
            lines.append(f"- {p.get('full_name') or p.get('short_name') or p.get('alias_id')} | SL {p.get('sl') or p.get('skill_level')} | win_pct {p.get('win_pct') or p.get('pa')} | ppm {p.get('ppm') or p.get('points_per_match')}")
    return "\n".join(lines)

def call_ollama(prompt: str, model: str = "phi3:mini", timeout: int = 30) -> str:
    """Call a local Ollama model with lean, fast defaults for phi3:mini."""
    # Keep prompts small and fast: limit ctx and tokens via env or model defaults.
    env = os.environ.copy()
    env.setdefault("OLLAMA_NUM_THREADS", "4")  # adjust to your physical cores
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
    try:
        result = subprocess.run(
            ["ollama", "run", model, "--verbose", "false"],
            input=prompt.encode('utf-8'),
            capture_output=True,
            timeout=timeout,
            env=env
        )
        if result.returncode != 0:
            logging.error(f"Ollama error ({result.returncode}): {result.stderr.decode(errors='ignore')}")
            return ""
        return result.stdout.decode(errors='ignore').strip()
    except FileNotFoundError:
        logging.error("Ollama binary not found. Install Ollama or adjust model runner.")
        return ""
    except subprocess.TimeoutExpired:
        logging.error("Ollama call timed out.")
        return ""

def build_ai_context(current_user=None):
    """Create a compact context string from league data for AI Q&A."""
    data = load_league_data()
    players = list((data.get('players') or {}).values())
    teams = list((data.get('teams') or {}).values())
    my_team = next((t for t in teams if normalize_team_name(t.get('team_name')) == MY_TEAM_KEY), None)

    def metric(p):
        for key in ('win_pct', 'percent_points_avail', 'pa', 'ppm', 'points_per_match'):
            val = p.get(key)
            if isinstance(val, (int, float)):
                return val
            try:
                return float(str(val).replace('%',''))
            except Exception:
                continue
        return 0

    top_players = sorted(players, key=metric, reverse=True)[:8]
    lines = []
    if my_team:
        lines.append(f"Team: {my_team.get('team_name')} | division: {my_team.get('division_name')} | rank: {my_team.get('rank') or '?'} | points: {my_team.get('points') or '?'}")
    if current_user and current_user.player_ref:
        slug = normalize_team_name(current_user.player_ref)
        found = next((p for p in players if normalize_team_name(p.get('full_name') or p.get('short_name') or '') == slug), None)
        if found:
            lines.append(f"User player: {found.get('full_name') or found.get('short_name')} | SL: {found.get('sl') or found.get('skill_level')} | win_pct: {found.get('win_pct') or found.get('pa')} | ppm: {found.get('ppm') or found.get('points_per_match')}")
    if top_players:
        lines.append("Top players (by win/ppm):")
        for p in top_players:
            lines.append(f"- {p.get('full_name') or p.get('short_name') or p.get('alias_id')} | SL {p.get('sl') or p.get('skill_level')} | win_pct {p.get('win_pct') or p.get('pa')} | ppm {p.get('ppm') or p.get('points_per_match')}")
    return "\n".join(lines)

def call_ollama(prompt: str, model: str = "phi3:mini", timeout: int = 45) -> str:
    """Call a local Ollama model and return its response text."""
    try:
        result = subprocess.run(
            ["ollama", "run", model],
            input=prompt.encode('utf-8'),
            capture_output=True,
            timeout=timeout
        )
        if result.returncode != 0:
            logging.error(f"Ollama error ({result.returncode}): {result.stderr.decode(errors='ignore')}")
            return ""
        return result.stdout.decode(errors='ignore').strip()
    except FileNotFoundError:
        logging.error("Ollama binary not found. Install Ollama or adjust model runner.")
        return ""
    except subprocess.TimeoutExpired:
        logging.error("Ollama call timed out.")
        return ""

def find_latest_dump():
    """Locate the most recent api_dump_v2.json under known capture roots.

    We look in:
    - data/apa_api_captures (default output)
    - scripts/scraper_sniffer/apa_api_captures (manual runs launched from that folder)
    """
    roots = [
        Path(DATA_DIR) / 'apa_api_captures',
        Path(__file__).parent / 'scripts' / 'scraper_sniffer' / 'apa_api_captures',
    ]
    candidates = []
    for root in roots:
        if root.exists():
            candidates.extend(root.rglob('api_dump_v2.json'))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

# Compute rank change for standings
def compute_rank_change(current, previous):
    # current, previous: list of dicts with 'team' and 'rank' keys
    changes = {}
    prev_ranks = {t['team']: i for i, t in enumerate(previous)}
    for i, t in enumerate(current):
        team = t['team']
        prev = prev_ranks.get(team, i)
        changes[team] = prev - i  # positive = moved up
    return changes




# --- League data helpers (builds old views from league_data_final_week11.json) ---
def load_league_data():
    """Load the unified league data JSON safely."""
    data = load_json(LEAGUE_DATA_FILENAME)
    if not isinstance(data, dict):
        logging.error('League data JSON is missing or invalid.')
        return {'meta': {}, 'divisions': {}, 'teams': {}, 'players': {}, 'matches': {}}
    # Ensure required keys exist
    for key in ['divisions', 'teams', 'players']:
        if key not in data:
            logging.error(f'League data JSON missing key: {key}')
            data.setdefault(key, {})
    data.setdefault('matches', {})

    # Normalize list-based payloads into id-keyed dicts
    def list_to_dict(items, id_key):
        if isinstance(items, dict):
            return items
        if not isinstance(items, list):
            return {}
        out = {}
        for obj in items:
            if not isinstance(obj, dict):
                continue
            oid = obj.get(id_key)
            if oid is None:
                continue
            out[str(oid)] = obj
        return out

    data['divisions'] = list_to_dict(data.get('divisions'), 'division_id')
    data['teams'] = list_to_dict(data.get('teams'), 'team_id')
    data['players'] = list_to_dict(data.get('players'), 'alias_id')
    data['matches'] = list_to_dict(data.get('matches'), 'match_id')

    # Ensure player ids/slug helpers for legacy files
    def simple_slug(s: str) -> str:
        if not s:
            return ''
        s = ''.join(ch.lower() if ch.isalnum() else '_' for ch in str(s))
        return re.sub(r'_+', '_', s).strip('_')

    for pid, p in list(data['players'].items()):
        p.setdefault('alias_id', pid)
        if not p.get('slug'):
            name = p.get('display_name') or p.get('full_name') or p.get('short_name') or pid
            p['slug'] = simple_slug(name)
        data['players'][pid] = p
    # Infer formats if missing (new JSON may omit format/type)
    try:
        divisions = data.get('divisions') or {}
        teams = data.get('teams') or {}
        fmt_map = {'EIGHT': '8-ball', '8': '8-ball', '9': '9-ball', 'NINE': '9-ball'}
        inferred = {}
        # First pass: infer from raw APA metadata on teams
        for tid, team in teams.items():
            div_id = team.get('division_id')
            fmt = team.get('format')
            if not fmt:
                raw_div = (((team.get('apa_meta') or {}).get('raw_team') or {}).get('division') or {})
                fmt = fmt_map.get(str(raw_div.get('type') or '').upper())
            if fmt and div_id:
                team['format'] = fmt
                div = divisions.get(div_id, {})
                if not div.get('format'):
                    div['format'] = fmt
                    div['type'] = fmt
                    divisions[div_id] = div
                inferred[div_id] = fmt
        # Second pass: fill remaining divisions by simple alternating guess
        div_ids_sorted = sorted(divisions.keys())
        fmt_cycle = ['8-ball', '9-ball']
        for idx, div_id in enumerate(div_ids_sorted):
            div = divisions.get(div_id) or {}
            fmt = div.get('format') or div.get('type')
            if not fmt:
                fmt = fmt_cycle[idx % len(fmt_cycle)]
            div['format'] = fmt
            div['type'] = fmt
            divisions[div_id] = div
            inferred[div_id] = fmt
        # Push inferred formats into teams and matches
        for tid, team in teams.items():
            if not team.get('format'):
                fmt = inferred.get(team.get('division_id'))
                if fmt:
                    team['format'] = fmt
        for mid, match in (data.get('matches') or {}).items():
            if not match.get('format'):
                fmt = inferred.get(match.get('division_id'))
                if fmt:
                    match['format'] = fmt
    except Exception as e:
        logging.warning(f"Format inference skipped: {e}")
    return data

def pretty_team_name(team_id: str, raw_name: str) -> str:
    """Normalize some team name quirks while keeping branding."""
    if 'cue_ligans' in str(team_id):
        return 'Cue-ligans'
    if 'ballerz_956' in str(team_id):
        return 'Ballerz 956'
    return raw_name

def team_display_name(team_id: str) -> str:
    teams = get_league_teams()
    team = teams.get(team_id) or teams.get(str(team_id)) or {}
    return pretty_team_name(team_id, team.get('name') or str(team_id))

def slug_for_logo(slug_value: str) -> str:
    if not slug_value:
        return ''
    cleaned = ''.join(ch for ch in str(slug_value).lower() if ch.isalnum() or ch in ('-', '_'))
    return cleaned.replace('_', '-').strip('-')

def get_team_logo_url(team_id: str) -> str:
    candidate = DEFAULT_TEAM_LOGO_URL
    if not team_id:
        return candidate
    teams = get_league_teams()
    team = teams.get(team_id) or teams.get(str(team_id)) or {}
    slug = team.get('slug') or normalize_team_name(team.get('name', ''))
    logo_slug = slug_for_logo(slug)
    if not logo_slug:
        return candidate
    for ext in TEAM_LOGO_EXTENSIONS:
        filename = f"{logo_slug}-logo.{ext}"
        path = TEAM_LOGO_DIR / filename
        if path.exists():
            try:
                rel = path.relative_to(Path(app.static_folder))
                return f"/static/{rel.as_posix()}"
            except ValueError:
                return candidate
    return candidate

def get_league_teams():
    league = load_league_data()
    return league.get('teams', {}) if isinstance(league, dict) else {}

def build_team_options(selected_id=None):
    teams = get_league_teams()
    options = []
    entries = []
    for tid, team in teams.items():
        name = pretty_team_name(tid, team.get('name') or team.get('team_name') or str(tid))
        fmt_raw = team.get('format') or ''
        fmt_priority = 0 if '8' in fmt_raw else 1
        entries.append((name, fmt_priority, fmt_raw, str(tid)))
    entries.sort(key=lambda x: (x[0], x[1]))
    seen_names = set()
    for name, _, _, tid in entries:
        if name in seen_names:
            continue
        seen_names.add(name)
        options.append({'id': tid, 'label': name, 'display': name})
    if selected_id:
        sid = str(selected_id)
        if sid not in {opt['id'] for opt in options}:
            team = teams.get(sid) or teams.get(selected_id) or {}
            name = pretty_team_name(sid, team.get('name') or team.get('team_name') or sid)
            options.append({'id': sid, 'label': name, 'display': name})
    return options

def build_standings_from_league(data):
    """Return a dict shaped like standings_week4.json using league_data."""
    divisions = data.get('divisions', {})
    teams = data.get('teams', {})
    result = {'eight_ball': [], 'nine_ball': []}

    def build_for_div(div_id):
        div = divisions.get(div_id, {})
        out = []
        for row in div.get('standings', []):
            team_id = row.get('team_id')
            team = teams.get(team_id, {})
            name = pretty_team_name(team_id, team.get('name', str(team_id)))
            # Fallback points from team session summary / APA raw standings
            row_points = row.get('points')
            if row_points in (None, ''):
                row_points = team.get('session_summary', {}).get('session_total_points')
                if row_points in (None, ''):
                    row_points = team.get('apa_meta', {}).get('raw_standings', {}).get('sessionTotalPoints')
            out.append({
                'rank': row.get('rank'),
                'team': name,
                'points': row_points or 0,
            })
        out.sort(key=lambda t: t.get('rank') or 0)
        return out

    # Fallback: map divisions by inferred format
    for div_id, div in divisions.items():
        fmt = (div or {}).get('format') or (div or {}).get('type') or ''
        if '8-ball' in fmt:
            result['eight_ball'] = build_for_div(div_id)
        elif '9-ball' in fmt:
            result['nine_ball'] = build_for_div(div_id)
    # Legacy ids
    result['eight_ball'] = result['eight_ball'] or build_for_div('thursday_brownsville_8')
    result['nine_ball'] = result['nine_ball'] or build_for_div('thursday_brownsville_9')

    # If standings still empty, derive from matches by summing team_scores
    if (not result['eight_ball']) or (not result['nine_ball']):
        matches = data.get('matches', {}) or {}
        points_by_div = {}
        for m in matches.values():
            div_id = m.get('division_id')
            fmt = (m.get('format') or '').lower()
            if not div_id:
                continue
            scores = m.get('team_scores') or {}
            ht, at = m.get('home_team_id'), m.get('away_team_id')
            if ht is None or at is None:
                continue
            points_by_div.setdefault(div_id, {}).setdefault(ht, 0)
            points_by_div.setdefault(div_id, {}).setdefault(at, 0)
            points_by_div[div_id][ht] += scores.get('home_total') or 0
            points_by_div[div_id][at] += scores.get('away_total') or 0
        for div_id, team_points in points_by_div.items():
            rows = []
            for tid, pts in team_points.items():
                team = teams.get(tid) or teams.get(str(tid), {})
                rows.append({'rank': None, 'team': pretty_team_name(tid, team.get('name', str(tid))), 'points': pts})
            rows.sort(key=lambda r: r['points'], reverse=True)
            for idx, row in enumerate(rows, start=1):
                row['rank'] = idx
            fmt = (divisions.get(div_id, {}) or {}).get('format', '').lower()
            if '8' in fmt and not result['eight_ball']:
                result['eight_ball'] = rows
            if '9' in fmt and not result['nine_ball']:
                result['nine_ball'] = rows
    return result

def build_rosters_from_league(data):
    """Return a list shaped like the old rosters.json using league_data."""
    divisions = data.get('divisions', {})
    teams = data.get('teams', {})
    players = data.get('players', {})
    session_name = data.get('meta', {}).get('session')
    base = {}  # normalized_team_name -> combined entry

    def make_player_row(pid, fmt_target):
        p = players.get(str(pid)) or players.get(pid) or {}
        full_name = p.get('display_name') or p.get('full_name') or p.get('short_name') or str(pid).replace('_', ' ').title()
        stats_block = p.get('stats') or {}
        stat = None
        # Map common keys
        key_map = {'8-ball': ['eight_ball', '8_ball', '8ball'], '9-ball': ['nine_ball', '9_ball', '9ball']}
        for k in key_map.get(fmt_target, []):
            if k in stats_block:
                stat = stats_block.get(k) or {}
                break
        if not stat:
            for val in stats_block.values():
                raw_typ = str(((val or {}).get('raw') or {}).get('__typename') or '')
                if fmt_target.startswith('8') and 'EightBall' in raw_typ:
                    stat = val; break
                if fmt_target.startswith('9') and 'NineBall' in raw_typ:
                    stat = val; break
            if not stat and 'unknown' in stats_block:
                stat = stats_block.get('unknown') or {}
        stat = stat or {}
        mw = stat.get('matches_won', 0)
        mp = stat.get('matches_played', 0) or stat.get('matches', 0) or stat.get('match_count', 0) or 0
        win_pct = stat.get('win_pct') or (mw / mp if mp else 0)
        ppm = stat.get('ppm') or stat.get('points_per_match') or stat.get('avg_points') or (p.get('extra') or {}).get('ppm') or 0.0
        pa = stat.get('pa') or stat.get('percent_points_avail') or (p.get('extra') or {}).get('pa') or 0.0
        try:
            sl_local = (p.get('current_skill_levels') or {}).get(fmt_target, 0)
        except Exception:
            sl_local = 0
        if not sl_local:
            sl_local = (p.get('extra') or {}).get('skillLevel') or 0
        return {
            'name': full_name,
            'id': p.get('apa_id'),
            'skill_level': sl_local,
            'matches': f"{mw}/{mp}" if mp else '0/0',
            'win_pct': f"{win_pct*100:.2f}%" if win_pct <= 1 else f"{win_pct:.2f}%",
            'ppm': round(ppm, 2),
            'pa': f"{pa*100:.2f}%" if pa <= 1 else f"{pa:.2f}%"
        }

    for team_id, team in teams.items():
        team_fmt = team.get('format')  # may be None
        norm = normalize_team_name(team.get('name', team_id))
        entry = base.setdefault(norm, {
            'team': pretty_team_name(team_id, team.get('name', str(team_id))),
            'home': team.get('home_location', ''),
            'eight_ball': [],
            'nine_ball': [],
        })

        roster_list = team.get('roster') or team.get('roster_alias_ids') or team.get('player_ids') or []
        for p_ref in roster_list:
            pid = p_ref.get('player_id') if isinstance(p_ref, dict) else p_ref
            p = players.get(str(pid)) or players.get(pid) or {}
            if not p:
                continue
            full_name = p.get('full_name') or p.get('short_name') or pid
            if not full_name:
                safe_pid = str(pid).replace('_', ' ')
                full_name = safe_pid.title()
            elif '_' in full_name and full_name == full_name.lower():
                full_name = full_name.replace('_', ' ').title()
            apa_id = p.get('apa_id')
            skill_levels = p.get('current_skill_levels', {})
            sl = 0
            if isinstance(skill_levels, dict):
                for k, v in skill_levels.items():
                    if v and ('8' in str(k) or '9' in str(k)):
                        sl = v
                        break
            # Find session stats for this team/format
            matches_won = matches_played = 0
            win_pct = ppm = pa = 0.0
            sessions_block = p.get('sessions', [])
            stats_block = p.get('stats') or {}
            def pick_stat(stats_dict, fmt_label):
                if fmt_label in stats_dict:
                    return stats_dict.get(fmt_label) or {}
                for val in stats_dict.values():
                    raw_typ = str(((val or {}).get('raw') or {}).get('__typename') or '')
                    if fmt_label.startswith('8') and 'EightBall' in raw_typ:
                        return val or {}
                    if fmt_label.startswith('9') and 'NineBall' in raw_typ:
                        return val or {}
                if 'unknown' in stats_dict:
                    return stats_dict.get('unknown') or {}
                if len(stats_dict) == 1:
                    return list(stats_dict.values())[0] or {}
                return {}
            # Normalize dict sessions into an iterable
            if isinstance(sessions_block, dict):
                sessions_iter = sessions_block.values()
            else:
                sessions_iter = sessions_block

            # Infer per-player formats
            inferred_formats = set()
            for key in (stats_block or {}):
                if '8' in str(key):
                    inferred_formats.add('8-ball')
                if '9' in str(key):
                    inferred_formats.add('9-ball')
            for stat in (stats_block or {}).values():
                raw_typ = str(((stat or {}).get('raw') or {}).get('__typename') or '')
                if 'NineBall' in raw_typ:
                    inferred_formats.add('9-ball')
                if 'EightBall' in raw_typ:
                    inferred_formats.add('8-ball')
            for k in (p.get('current_skill_levels') or {}):
                if '8' in str(k):
                    inferred_formats.add('8-ball')
                if '9' in str(k):
                    inferred_formats.add('9-ball')
            if team_fmt in ('8-ball', '9-ball'):
                inferred_formats = {team_fmt}
            if not inferred_formats:
                inferred_formats = {'8-ball', '9-ball'}

            for fmt_target in ('8-ball', '9-ball'):
                if fmt_target not in inferred_formats:
                    continue
                matches_won = matches_played = 0
                win_pct = ppm = pa = 0.0
                sl_local = sl
                for s in sessions_iter:
                    if not isinstance(s, dict):
                        continue
                    if s.get('session') == session_name and s.get('format') == fmt_target and s.get('team_id') == team_id:
                        matches_won = s.get('matches_won', 0)
                        matches_played = s.get('matches_played', 0)
                        try:
                            win_pct = float(s.get('win_pct', 0.0))
                        except Exception:
                            win_pct = 0.0
                        ppm = float(s.get('points_per_match', 0.0) or 0.0)
                        try:
                            pa = float(s.get('percent_points_avail', 0.0) or 0.0)
                        except Exception:
                            pa = 0.0
                        sl_local = s.get('skill_level') or sl_local
                        break
                # Fallback to stats block (new schema)
                if matches_played == 0 and stats_block:
                    stat = pick_stat(stats_block, fmt_target)
                    matches_played = stat.get('matches_played', 0) or stat.get('matches', 0) or 0
                    matches_won = stat.get('matches_won', 0)
                    ppm = stat.get('ppm') or stat.get('points_per_match') or 0.0
                    pa = stat.get('pa') or stat.get('percent_points_avail') or 0.0
                    win_pct = stat.get('win_pct') or (matches_won / matches_played if matches_played else 0)
                    sl_local = stat.get('skill_level') or sl_local
                matches_str = f"{matches_won}/{matches_played}" if matches_played else '0/0'
                player_row = {
                    'name': full_name,
                    'pid': pid,
                    'id': apa_id,
                    'skill_level': sl_local,
                    'matches': matches_str,
                    'win_pct': f"{win_pct * 100:.2f}%" if win_pct <= 1 else f"{win_pct:.2f}%",
                    'ppm': round(ppm, 2),
                    'pa': f"{pa * 100:.2f}%" if pa <= 1 else f"{pa:.2f}%",
                }
                if fmt_target == '8-ball':
                    entry['eight_ball'].append(player_row)
                elif fmt_target == '9-ball':
                    entry['nine_ball'].append(player_row)

    return list(base.values())

# ---------- LAB helper utilities ----------
def get_division_label(div_id: str, div: dict) -> str:
    return div.get('name') or div_id.replace('_', ' ').title()

def get_format_for_division(div_id: str) -> str:
    return '9-ball' if '9' in str(div_id) else '8-ball'

def normalize_percent(value):
    try:
        if isinstance(value, str) and '%' in value:
            value = value.replace('%', '')
        val = float(value)
        if val > 1.0:
            return val / 100.0 if val > 10 else val
        return val
    except Exception:
        return 0.0

def player_metrics(player: dict, fmt: str):
    sessions_raw = player.get('sessions', {}) or {}
    # sessions may be a dict keyed by format, or a list of session dicts
    session_fmt = {}
    if isinstance(sessions_raw, dict):
        session_fmt = sessions_raw.get(fmt) or sessions_raw.get(fmt.replace('-', '_'), {}) or {}
    elif isinstance(sessions_raw, list):
        for s in sessions_raw:
            if not isinstance(s, dict):
                continue
            s_fmt = s.get('format') or s.get('type')
            if s_fmt and fmt in str(s_fmt):
                session_fmt = s
                break
        if not session_fmt and sessions_raw:
            session_fmt = sessions_raw[0] if isinstance(sessions_raw[0], dict) else {}
    else:
        session_fmt = {}
    skill_levels = player.get('current_skill_levels', {})
    sl = skill_levels.get(fmt) or 0
    win_pct = normalize_percent(session_fmt.get('win_pct', 0))
    ppm = float(session_fmt.get('points_per_match', session_fmt.get('ppm', 0)) or 0)
    pa = normalize_percent(session_fmt.get('percent_points_avail', session_fmt.get('pa', 0)))
    return {'sl': sl or 0, 'win_pct': win_pct, 'ppm': ppm, 'pa': pa}


def matches_format(candidate_fmt: str, target_fmt: str) -> bool:
    if not candidate_fmt or not target_fmt:
        return False
    cand = candidate_fmt.lower()
    tgt = target_fmt.lower()
    if tgt in cand:
        return True
    short = tgt.split('-')[0]
    return bool(short and short in cand)


def canonical_format(param: str) -> str:
    if not param:
        return '8-ball'
    norm = str(param).lower()
    if '9' in norm:
        return '9-ball'
    return '8-ball'


def pick_stat_block(stats_block: dict, fmt: str) -> dict:
    if not isinstance(stats_block, dict):
        return {}
    candidates = [fmt, fmt.replace('-', '_'), fmt.replace('-', ''), fmt.split('-')[0]]
    for key in candidates:
        if not key:
            continue
        block = stats_block.get(key)
        if isinstance(block, dict) and block:
            return block
    if 'unknown' in stats_block and isinstance(stats_block['unknown'], dict):
        return stats_block['unknown']
    for val in stats_block.values():
        if not isinstance(val, dict):
            continue
        raw_typ = str(((val or {}).get('raw') or {}).get('__typename') or '').lower()
        if matches_format(raw_typ, fmt):
            return val
    return {}


def find_session_block(player: dict, fmt: str, session_name: str) -> dict:
    sessions = player.get('sessions') or {}

    def consider(entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        entry_fmt = (entry.get('format') or '').lower()
        if entry_fmt and not matches_format(entry_fmt, fmt):
            return False
        entry_session = (entry.get('session') or entry.get('session_name') or '')
        if session_name and entry_session and session_name != entry_session:
            return False
        return True

    if isinstance(sessions, dict):
        for val in sessions.values():
            if consider(val):
                return val
        entry = sessions.get(fmt) or sessions.get(fmt.replace('-', '_')) or sessions.get(fmt.split('-')[0])
        if isinstance(entry, dict) and consider(entry):
            return entry
    elif isinstance(sessions, list):
        for val in sessions:
            if consider(val):
                return val
    stats_block = player.get('stats') or {}
    entry = pick_stat_block(stats_block, fmt)
    return entry or {}


def safe_int(value):
    try:
        return int(value)
    except Exception:
        return 0


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


HIGHLIGHT_MAP = {
    '8-ball': {
        'eight_ball_break_and_runs': 'Break & Run',
        'eight_on_breaks': '8-On-The-Break',
        'rackless': 'Rackless Nights',
        'skunks': 'Skunks'
    },
    '9-ball': {
        'nine_ball_break_and_runs': 'Break & Run',
        'nine_on_snaps': '9-On-The-Snap',
        'rackless': 'Rackless Nights',
        'skunks': 'Skunks'
    }
}


def collect_highlight_counts(player: dict, fmt: str, session_name: str) -> dict:
    counts = {}
    highlights = player.get('session_highlights') or []
    target_map = HIGHLIGHT_MAP.get(fmt, {})
    for entry in highlights:
        if not isinstance(entry, dict):
            continue
        entry_fmt = (entry.get('format') or '').lower()
        entry_session = (entry.get('session_name') or entry.get('session') or '')
        if not matches_format(entry_fmt, fmt):
            continue
        if session_name and entry_session and session_name != entry_session:
            continue
        for key, label in target_map.items():
            value = safe_int(entry.get(key))
            if value <= 0:
                continue
            counts[label] = counts.get(label, 0) + value
    return counts


def summarize_player_stats(player: dict, fmt: str, session_name: str) -> dict:
    entry = find_session_block(player, fmt, session_name)
    matches_played = safe_int(entry.get('matches_played') or entry.get('matches') or entry.get('match_count'))
    matches_won = safe_int(entry.get('matches_won') or entry.get('wins'))
    win_pct_raw = entry.get('win_pct')
    if win_pct_raw in (None, '') and matches_played:
        win_pct_raw = matches_won / matches_played
    win_pct = normalize_percent(win_pct_raw or 0)
    ppm = safe_float(entry.get('points_per_match') or entry.get('ppm') or 0.0)
    pa = normalize_percent(entry.get('percent_points_avail') or entry.get('pa') or 0.0)
    total_points = entry.get('session_total_points') or entry.get('total_points') or entry.get('points') or \
        (ppm * matches_played if matches_played else 0.0)
    return {
        'matches_played': matches_played,
        'matches_won': matches_won,
        'win_pct': win_pct,
        'ppm': ppm,
        'pa': pa,
        'total_points': round(float(total_points or 0.0), 2)
    }


def select_division_for_format(data: dict, fmt: str, division_id: str = None, prefer_keyword: str = 'Thursday Brownsville'):
    divisions = data.get('divisions') or {}
    fmt_key = fmt.lower()
    if division_id:
        candidate = divisions.get(division_id) or divisions.get(division_id.lower())
        if candidate:
            cand_fmt = (candidate.get('format') or '').lower()
            if matches_format(cand_fmt, fmt_key):
                return division_id, candidate
    matches = []
    for div_id, div in divisions.items():
        cand_fmt = (div.get('format') or '').lower()
        if matches_format(cand_fmt, fmt_key):
            matches.append((div_id, div))
    if matches:
        keyword = (prefer_keyword or '').lower()
        for div_id, div in matches:
            if keyword and keyword in (div.get('name') or '').lower():
                return div_id, div
        return matches[0]
    if divisions:
        first_id = next(iter(divisions))
        return first_id, divisions[first_id]
    return None, {}


def gather_mvp_players(data: dict, division_id: str, fmt: str, session_name: str) -> list:
    teams = data.get('teams') or {}
    players = data.get('players') or {}
    division = (data.get('divisions') or {}).get(division_id) or {}
    team_ids = division.get('team_ids') or []
    if not team_ids:
        team_ids = [tid for tid, team in teams.items() if team.get('division_id') == division_id]
    records = []
    for team_id in team_ids:
        team = teams.get(team_id, {})
        roster = team.get('roster') or []
        for ref in roster:
            pid = ref.get('player_id') if isinstance(ref, dict) else ref
            player = players.get(pid) or players.get(str(pid)) or {}
            if not player:
                continue
            stat = summarize_player_stats(player, fmt, session_name)
            highlight_counts = collect_highlight_counts(player, fmt, session_name)
            parsed_name = player.get('display_name') or player.get('full_name') or player.get('short_name') or str(pid)
            records.append({
                'alias_id': player.get('alias_id') or player.get('id') or pid,
                'player_id': pid,
                'player_name': parsed_name,
                'team_id': team_id,
                'team_name': pretty_team_name(team_id, team.get('name') or str(team_id)),
                'format': fmt,
                'matches_played': stat['matches_played'],
                'matches_won': stat['matches_won'],
                'win_pct': stat['win_pct'],
                'ppm': stat['ppm'],
                'pa': stat['pa'],
                'total_points': stat['total_points'],
                'patch_counts': highlight_counts,
                'session_name': session_name,
                'is_cue_ligan': normalize_team_name(team.get('name', team_id)) == MY_TEAM_KEY
            })
    return records

def team_roster_ids(team: dict):
    ids = []
    if not isinstance(team, dict):
        return ids
    roster_sources = []
    roster_sources.extend(team.get('roster') or [])
    roster_sources.extend(team.get('roster_alias_ids') or [])
    roster_sources.extend(team.get('player_ids') or [])
    for ref in roster_sources:
        if isinstance(ref, dict):
            pid = ref.get('player_id') or ref.get('id')
        else:
            pid = ref
        if pid:
            ids.append(pid)
    return ids

def compute_team_rating(team_id: str, fmt: str, league: dict):
    teams = league.get('teams', {})
    players = league.get('players', {})
    team = teams.get(team_id, {})
    roster_ids = team_roster_ids(team)
    metrics = []
    for pid in roster_ids:
        p = players.get(pid) or players.get(str(pid), {})
        if not p:
            continue
        metrics.append(player_metrics(p, fmt))
    if not metrics:
        return {'ppm_avg': 0, 'pa_avg': 0, 'rating': 0, 'count': 0}
    ppm_avg = sum(m['ppm'] for m in metrics) / len(metrics)
    pa_avg = sum(m['pa'] for m in metrics) / len(metrics)
    rating = (ppm_avg * 0.7) + (pa_avg * 0.3 * 20)
    return {'ppm_avg': ppm_avg, 'pa_avg': pa_avg, 'rating': rating, 'count': len(metrics)}

def player_match_analytics(pid: str, fmt: str, league: dict):
    matches = league.get('matches', {}) or {}
    players = league.get('players', {}) or {}
    me = players.get(pid) or players.get(str(pid), {})
    my_sl = (me.get('current_skill_levels') or {}).get(fmt, 0)
    entries = []
    for m in matches.values():
        if m.get('format') not in (fmt, fmt.replace('_', '-')):
            continue
        date = m.get('date')
        sets = m.get('sets', []) or []
        for s in sets:
            home_id = s.get('home_player_id')
            away_id = s.get('away_player_id')
            if pid not in (home_id, away_id):
                continue
            opp_id = away_id if pid == home_id else home_id
            opp = players.get(opp_id, {})
            opp_sl = (opp.get('current_skill_levels') or {}).get(fmt, 0) or 0
            my_pts = s.get('home_points' if pid == home_id else 'away_points', 0) or 0
            opp_pts = s.get('away_points' if pid == home_id else 'home_points', 0) or 0
            outcome = 'W' if my_pts > opp_pts else ('T' if my_pts == opp_pts else 'L')
            entries.append({'pts': float(my_pts), 'opp_sl': float(opp_sl), 'outcome': outcome, 'date': date})
    if not entries:
        return {
            'avg': 0, 'recent_avg': 0, 'var': 0,
            'avg_high': 0, 'avg_equal': 0, 'avg_low': 0,
            'wr_high': 0, 'wr_equal': 0, 'wr_low': 0,
            'count': 0
        }
    total = sum(e['pts'] for e in entries)
    avg = total / len(entries)
    recent = sorted(entries, key=lambda x: x.get('date') or '')[-3:]
    recent_avg = sum(e['pts'] for e in recent) / len(recent) if recent else avg
    var = sum((e['pts'] - avg) ** 2 for e in entries) / len(entries)
    def bucket(cond):
        subset = [e for e in entries if cond(e)]
        if not subset:
            return 0, 0
        avg_b = sum(e['pts'] for e in subset) / len(subset)
        wr = sum(1 for e in subset if e['outcome'] == 'W') / len(subset)
        return avg_b, wr
    avg_low, wr_low = bucket(lambda e: e['opp_sl'] < my_sl - 0.5)
    avg_equal, wr_equal = bucket(lambda e: abs(e['opp_sl'] - my_sl) <= 0.5)
    avg_high, wr_high = bucket(lambda e: e['opp_sl'] > my_sl + 0.5)
    return {
        'avg': avg,
        'recent_avg': recent_avg,
        'var': var,
        'avg_high': avg_high, 'avg_equal': avg_equal, 'avg_low': avg_low,
        'wr_high': wr_high, 'wr_equal': wr_equal, 'wr_low': wr_low,
        'count': len(entries),
    }

# --- APA race helpers (simplified tables) ---
RACE_8 = {
    1: 2, 2: 2, 3: 2, 4: 3, 5: 4, 6: 4, 7: 5
}
RACE_9 = {
    1: 14, 2: 19, 3: 25, 4: 31, 5: 38, 6: 46, 7: 55, 8: 65, 9: 75
}

def race_length(fmt: str, sl: float) -> int:
    sl_int = int(round(sl or 0))
    if fmt.startswith('8'):
        return RACE_8.get(sl_int, 4)
    return RACE_9.get(sl_int, 40)

def player_race_profile(pid: str, fmt: str, league: dict):
    """Compute race-based efficiencies and hill/hill behavior."""
    players = league.get('players', {}) or {}
    matches = league.get('matches', {}) or {}
    p = players.get(pid) or players.get(str(pid), {})
    sl = (p.get('current_skill_levels') or {}).get(fmt, 0)
    race_to = race_length(fmt, sl)
    stats = {
        'race_eff': 0, 'hill_attempts': 0, 'hill_wins': 0,
        'comeback_wins': 0, 'collapse_losses': 0,
        'avg_margin': 0, 'count': 0
    }
    margins = []
    effs = []
    for m in matches.values():
        if m.get('format') not in (fmt, fmt.replace('_', '-')):
            continue
        for s in m.get('sets', []) or []:
            home_id = s.get('home_player_id')
            away_id = s.get('away_player_id')
            if pid not in (home_id, away_id):
                continue
            my_pts = s.get('home_points' if pid == home_id else 'away_points', 0) or 0
            opp_pts = s.get('away_points' if pid == home_id else 'home_points', 0) or 0
            opp = players.get(away_id if pid == home_id else home_id, {})
            opp_sl = (opp.get('current_skill_levels') or {}).get(fmt, 0)
            opp_race = race_length(fmt, opp_sl)
            total_race = race_to if fmt.startswith('8') else race_to
            eff = (float(my_pts) / float(total_race)) if total_race else 0
            effs.append(eff)
            margin = my_pts - opp_pts
            margins.append(margin)
            # Hill/hill approximation: near final rack/points
            if fmt.startswith('8'):
                if my_pts >= race_to - 1 or opp_pts >= opp_race - 1:
                    stats['hill_attempts'] += 1
                    if my_pts > opp_pts:
                        stats['hill_wins'] += 1
            else:
                if my_pts >= race_to - 5 or opp_pts >= opp_race - 5:
                    stats['hill_attempts'] += 1
                    if my_pts > opp_pts:
                        stats['hill_wins'] += 1
            # Comeback / collapse heuristics
            if opp_pts - my_pts >= (race_to * 0.3) and my_pts > opp_pts:
                stats['comeback_wins'] += 1
            if my_pts - opp_pts >= (race_to * 0.3) and my_pts < opp_pts:
                stats['collapse_losses'] += 1
            stats['count'] += 1
    stats['race_eff'] = sum(effs) / len(effs) if effs else 0
    stats['avg_margin'] = sum(margins) / len(margins) if margins else 0
    return stats

def find_player_team(pid: str, fmt: str, league: dict):
    teams = league.get('teams', {}) or {}
    for tid, t in teams.items():
        if t.get('format') and fmt not in t.get('format'):
            continue
        for ref in t.get('roster', []) or []:
            rid = ref.get('player_id') if isinstance(ref, dict) else ref
            if rid == pid:
                return tid
    return None

def player_matches(pid: str, fmt: str, league: dict):
    matches = league.get('matches', {}) or {}
    players = league.get('players', {}) or {}
    records = []
    for m in matches.values():
        if m.get('format') not in (fmt, fmt.replace('_', '-')):
            continue
        date = m.get('date', '')
        for s in m.get('sets', []) or []:
            home_id = s.get('home_player_id')
            away_id = s.get('away_player_id')
            if pid not in (home_id, away_id):
                continue
            my_pts = s.get('home_points' if pid == home_id else 'away_points', 0) or 0
            opp_pts = s.get('away_points' if pid == home_id else 'home_points', 0) or 0
            opp_id = away_id if pid == home_id else home_id
            opp = players.get(opp_id, {})
            opp_sl = (opp.get('current_skill_levels') or {}).get(fmt, 0)
            records.append({
                'date': date,
                'points_for': float(my_pts),
                'points_against': float(opp_pts),
                'opp_sl': float(opp_sl),
                'outcome': 'W' if my_pts > opp_pts else ('T' if my_pts == opp_pts else 'L')
            })
    records.sort(key=lambda r: r.get('date', ''))
    return records

def build_player_trend(pid: str, fmt: str, league: dict, recent_count: int = 5):
    players = league.get('players', {}) or {}
    p = players.get(pid) or players.get(str(pid), {})
    base = player_metrics(p, fmt)
    session_stats = {
        'win_pct': base['win_pct'],
        'ppm': base['ppm'],
        'pa': base['pa'],
        'sl': base['sl']
    }
    records = player_matches(pid, fmt, league)
    recent = records[-recent_count:] if recent_count and records else records
    def win_pct_recs(recs):
        if not recs:
            return 0
        return sum(1 for r in recs if r['outcome']=='W')/len(recs)
    def avg_ppm(recs):
        if not recs:
            return 0
        return sum(r['points_for'] for r in recs)/len(recs)
    def avg_opp_sl(recs):
        if not recs:
            return 0
        return sum(r['opp_sl'] for r in recs)/len(recs)
    recent_stats = {
        'win_pct': win_pct_recs(recent),
        'ppm': avg_ppm(recent),
        'opp_sl': avg_opp_sl(recent),
        'count': len(recent)
    }
    # Opponent SL breakdown
    lower = [r for r in records if r['opp_sl'] < base['sl'] - 0.5]
    equal = [r for r in records if abs(r['opp_sl'] - base['sl']) <= 0.5]
    higher = [r for r in records if r['opp_sl'] > base['sl'] + 0.5]
    band_stats = []
    for label, recs in [('lower', lower), ('equal', equal), ('higher', higher)]:
        band_stats.append({
            'band': label,
            'win_pct': win_pct_recs(recs),
            'ppm': avg_ppm(recs),
            'count': len(recs)
        })
    # Team averages
    team_id = find_player_team(pid, fmt, league)
    team_avgs = {'ppm':0,'pa':0,'win_pct':0}
    if team_id:
        team_roster = get_roster_for_team(team_id, fmt, league)
        if team_roster:
            team_avgs['ppm'] = sum(r['ppm'] for r in team_roster)/len(team_roster)
            team_avgs['pa'] = sum(r['pa'] for r in team_roster)/len(team_roster)
            team_avgs['win_pct'] = sum(r['win_pct'] for r in team_roster)/len(team_roster)
    # trend label
    diff_ppm = recent_stats['ppm'] - session_stats['ppm']
    trend_label = 'Stable'
    if diff_ppm > 1:
        trend_label = 'Trending Up'
    elif diff_ppm < -1:
        trend_label = 'Trending Down'
    summary = f"{p.get('full_name', pid)} is {trend_label.lower()}. Recent PPM {recent_stats['ppm']:.1f} vs season {session_stats['ppm']:.1f}; team avg {team_avgs['ppm']:.1f}. "
    summary += f"Against lower SL: {band_stats[0]['win_pct']*100:.0f}% wins; equal: {band_stats[1]['win_pct']*100:.0f}%; higher: {band_stats[2]['win_pct']*100:.0f}%."
    return {
        'player': p.get('full_name', pid),
        'team_id': team_id,
        'format': fmt,
        'session': session_stats,
        'recent': recent_stats,
        'team': team_avgs,
        'bands': band_stats,
        'trend_label': trend_label,
        'diff_ppm': diff_ppm,
        'summary': summary,
        'sl': session_stats['sl']
    }

def build_sl_tracker(pid: str, fmt: str, league: dict, recent_count: int = 5):
    players = league.get('players', {}) or {}
    p = players.get(pid) or players.get(str(pid), {})
    base = player_metrics(p, fmt)
    session = {
        'sl': base['sl'],
        'win_pct': base['win_pct'],
        'ppm': base['ppm'],
        'pa': base['pa'],
    }
    matches = player_matches(pid, fmt, league)
    total_matches = len(matches)
    recent = matches[-recent_count:] if recent_count and matches else matches
    def avg(lst, key):
        return sum(item[key] for item in lst)/len(lst) if lst else 0
    recent_stats = {
        'win_pct': sum(1 for m in recent if m['outcome']=='W')/len(recent) if recent else 0,
        'ppm': avg(recent, 'points_for'),
        'opp_sl': avg(recent, 'opp_sl'),
        'count': len(recent)
    }
    # Expected scoring vs race length
    race_len = race_length(fmt, session['sl'])
    performance = []
    for m in matches:
        expected = race_len * (0.7 if m['outcome']=='W' else 0.5)
        performance.append({
            'date': m.get('date'),
            'opp_sl': m['opp_sl'],
            'points': m['points_for'],
            'outcome': m['outcome'],
            'delta': m['points_for'] - expected
        })
    lower = [m for m in matches if m['opp_sl'] < session['sl'] - 0.5]
    equal = [m for m in matches if abs(m['opp_sl'] - session['sl']) <= 0.5]
    higher = [m for m in matches if m['opp_sl'] > session['sl'] + 0.5]
    def band_stat(recs):
        if not recs: return {'win_pct':0,'ppm':0,'count':0}
        return {
            'win_pct': sum(1 for r in recs if r['outcome']=='W')/len(recs),
            'ppm': avg(recs, 'points_for'),
            'count': len(recs)
        }
    bands = {
        'lower': band_stat(lower),
        'equal': band_stat(equal),
        'higher': band_stat(higher)
    }
    # Team averages
    team_id = find_player_team(pid, fmt, league)
    team_avgs = {'ppm':0,'pa':0,'win_pct':0}
    if team_id:
        team_roster = get_roster_for_team(team_id, fmt, league)
        if team_roster:
            team_avgs['ppm'] = sum(r['ppm'] for r in team_roster)/len(team_roster)
            team_avgs['pa'] = sum(r['pa'] for r in team_roster)/len(team_roster)
            team_avgs['win_pct'] = sum(r['win_pct'] for r in team_roster)/len(team_roster)
    # Momentum and status
    diff_ppm = recent_stats['ppm'] - session['ppm']
    diff_win = recent_stats['win_pct'] - session['win_pct']
    status = 'Stable'
    if diff_ppm > 1 and diff_win > 0.05:
        status = 'Likely to move up'
    elif diff_ppm < -1 and diff_win < -0.05:
        status = 'Likely to move down'
    elif abs(diff_ppm) > 0.5:
        status = 'Borderline'
    summary = f"{p.get('full_name', pid)} is {status.lower()} for SL changes. Recent PPM {recent_stats['ppm']:.1f} vs session {session['ppm']:.1f}; win% {recent_stats['win_pct']*100:.0f}%."
    if bands['higher']['win_pct'] > bands['equal']['win_pct']:
        summary += " Performs well vs higher SL opponents."
    if bands['lower']['win_pct'] < 0.4:
        summary += " Needs improvement vs lower SL opponents."
    return {
        'player': p.get('full_name', pid),
        'format': fmt,
        'session': session,
        'recent': recent_stats,
        'team': team_avgs,
        'status': status,
        'matches': performance,
        'bands': bands,
        'total_matches': total_matches,
        'summary': summary
    }

def compute_team_power(fmt: str, league: dict, recent_weeks: int = 3):
    teams = league.get('teams', {}) or {}
    matches = league.get('matches', {}) or {}
    rankings = []
    for tid, t in teams.items():
        if fmt not in t.get('format', ''):
            continue
        roster = get_roster_for_team(tid, fmt, league)
        if not roster:
            continue
        avg_ppm = sum(p['ppm'] for p in roster)/len(roster)
        avg_pa = sum(p['pa'] for p in roster)/len(roster)
        avg_win = sum(p['win_pct'] for p in roster)/len(roster)
        # momentum from recent matches
        recent_pts = []
        for m in matches.values():
            if m.get('format') not in (fmt, fmt.replace('_','-')):
                continue
            if m.get('home_team_id') == tid:
                recent_pts.append(m.get('team_scores', {}).get('home_total', 0) or 0)
            elif m.get('away_team_id') == tid:
                recent_pts.append(m.get('team_scores', {}).get('away_total', 0) or 0)
        recent_pts = recent_pts[-recent_weeks:] if recent_pts else []
        momentum = sum(recent_pts)/len(recent_pts) if recent_pts else 0
        strength = avg_ppm * 0.6 + avg_pa*100*0.2 + avg_win*100*0.2
        rating = strength + (momentum*0.3)
        trend = 'flat'
        if recent_pts:
            recent_avg = momentum
            if recent_avg > avg_ppm + 1:
                trend = 'up'
            elif recent_avg < avg_ppm - 1:
                trend = 'down'
        rankings.append({
            'id': tid,
            'name': pretty_team_name(tid, t.get('name', tid)),
            'rating': rating,
            'win_pct': avg_win,
            'ppm': avg_ppm,
            'pa': avg_pa,
            'strength': strength,
            'momentum': momentum,
            'trend': trend
        })
    rankings.sort(key=lambda r: r['rating'], reverse=True)
    return rankings

def compute_player_power(fmt: str, league: dict, scope_team: str = None, recent_count: int = 5):
    players = league.get('players', {}) or {}
    rankings = []
    for pid, p in players.items():
        if scope_team:
            team_id = find_player_team(pid, fmt, league)
            if team_id != scope_team:
                continue
        base = player_metrics(p, fmt)
        if base['sl'] == 0:
            continue
        recs = player_matches(pid, fmt, league)
        recent = recs[-recent_count:] if recs else []
        def avg(lst, key):
            return sum(item[key] for item in lst)/len(lst) if lst else 0
        win_recent = sum(1 for r in recent if r['outcome']=='W')/len(recent) if recent else base['win_pct']
        ppm_recent = avg(recent, 'points_for') if recent else base['ppm']
        opp_sl_avg = avg(recent, 'opp_sl') if recent else 0
        strength = (base['ppm'] * 1.6) + (base['pa']*100*0.3) + (base['win_pct']*100*0.3)
        momentum = (ppm_recent * 0.8) + (win_recent*100*0.2)
        # Performance vs higher SL
        rec_high = [r for r in recs if r['opp_sl'] > base['sl'] + 0.5]
        perf_high = sum(1 for r in rec_high if r['outcome']=='W')/len(rec_high) if rec_high else base['win_pct']
        rating = strength + momentum + (perf_high*50)
        trend = 'flat'
        if ppm_recent > base['ppm'] + 1:
            trend = 'up'
        elif ppm_recent < base['ppm'] - 1:
            trend = 'down'
        rankings.append({
            'id': pid,
            'name': p.get('full_name', pid),
            'rating': rating,
            'win_pct': base['win_pct'],
            'ppm': base['ppm'],
            'pa': base['pa'],
            'opp_sl': opp_sl_avg,
            'momentum': ppm_recent,
            'trend': trend,
            'sl': base['sl']
        })
    rankings.sort(key=lambda r: r['rating'], reverse=True)
    return rankings

def compute_opponent_scout(fmt: str, opp_team_id: str, league: dict, our_team_id: str = None):
    teams = league.get('teams', {}) or {}
    matches = league.get('matches', {}) or {}
    standings = league.get('divisions', {}).get('thursday_brownsville_8' if fmt == '8-ball' else 'thursday_brownsville_9', {}).get('standings', [])
    standings_map = {row.get('team_id'): row for row in standings}
    roster = get_roster_for_team(opp_team_id, fmt, league)
    if not roster:
        return {}
    avg_ppm = sum(p['ppm'] for p in roster)/len(roster)
    avg_pa = sum(p['pa'] for p in roster)/len(roster)
    avg_win = sum(p['win_pct'] for p in roster)/len(roster)
    dist = {'low':0,'mid':0,'high':0}
    for p in roster:
        if p['sl'] <= 3:
            dist['low'] += 1
        elif p['sl'] <=5:
            dist['mid'] += 1
        else:
            dist['high'] += 1
    roster_sorted = sorted(roster, key=lambda p: (p['ppm'], p['pa'], p['win_pct']), reverse=True)
    top_perf = roster_sorted[:3]
    weak_links = [p for p in roster_sorted if p['ppm'] < avg_ppm*0.8][:3]
    # momentum from recent team totals
    team_scores = []
    for m in matches.values():
        if m.get('format') not in (fmt, fmt.replace('_','-')):
            continue
        if m.get('home_team_id') == opp_team_id:
            team_scores.append(m.get('team_scores', {}).get('home_total', 0) or 0)
        elif m.get('away_team_id') == opp_team_id:
            team_scores.append(m.get('team_scores', {}).get('away_total', 0) or 0)
    recent_scores = team_scores[-3:] if team_scores else []
    momentum = sum(recent_scores)/len(recent_scores) if recent_scores else 0
    trend = 'Stable'
    if momentum > avg_ppm*1.2:
        trend = 'Rising'
    elif momentum < avg_ppm*0.8:
        trend = 'Falling'
    # lineup tendencies: appearance counts
    appearance = {}
    for m in matches.values():
        if m.get('format') not in (fmt, fmt.replace('_','-')):
            continue
        for s in m.get('sets', []) or []:
            for pid_key in ('home_player_id','away_player_id'):
                pid = s.get(pid_key)
                if pid in [p['id'] for p in roster]:
                    appearance[pid] = appearance.get(pid, 0) + 1
    starters = sorted(appearance.items(), key=lambda x: x[1], reverse=True)[:2]
    # matchup warnings vs our roster
    warning = []
    if our_team_id:
        our_roster = get_roster_for_team(our_team_id, fmt, league)
        our_avg_sl = sum(p['sl'] for p in our_roster)/len(our_roster) if our_roster else 0
        for p in top_perf:
            if p['sl'] >= our_avg_sl+1:
                warning.append(f"{p['name']} (SL{p['sl']}) can pressure higher SL matchups.")
    snapshot = {
        'name': pretty_team_name(opp_team_id, teams.get(opp_team_id, {}).get('name', opp_team_id)),
        'points': standings_map.get(opp_team_id, {}).get('points', 0),
        'rank': standings_map.get(opp_team_id, {}).get('rank'),
        'avg_ppm': avg_ppm,
        'avg_pa': avg_pa,
        'avg_win': avg_win,
        'momentum': momentum,
        'trend': trend
    }
    return {
        'snapshot': snapshot,
        'roster': roster,
        'top': top_perf,
        'weak': weak_links,
        'distribution': dist,
        'appearance': starters,
        'warnings': warning,
    }

def player_rating_enhanced(pid: str, fmt: str, league: dict, opp_sls=None):
    players = league.get('players', {}) or {}
    p = players.get(pid, {})
    base = player_metrics(p, fmt)
    matches = player_match_analytics(pid, fmt, league)
    race_prof = player_race_profile(pid, fmt, league)
    opp_sls = opp_sls or []
    # Opponent scaling: compare to median opponent SL
    opp_median = 0
    if opp_sls:
        opp_sorted = sorted(opp_sls)
        opp_median = opp_sorted[len(opp_sorted)//2]
    sl_gap = base['sl'] - opp_median
    matchup_bias = 0
    if sl_gap < -1 and matches['avg_high']:
        matchup_bias += (matches['avg_high'] - matches['avg']) * 0.5
    elif sl_gap > 1 and matches['avg_low']:
        matchup_bias += (matches['avg_low'] - matches['avg']) * 0.5
    else:
        matchup_bias += (matches['avg_equal'] - matches['avg']) * 0.3

    trend = (matches['recent_avg'] - matches['avg']) * 0.5
    consistency = -min(5, matches['var'] * 0.05)  # penalize high variance lightly

    # Hill confidence
    hill_rate = (race_prof['hill_wins'] / race_prof['hill_attempts']) if race_prof['hill_attempts'] else 0

    rating = (
        (base['ppm'] * 2.0) +
        (base['pa'] * 100 * 0.4) +
        (base['win_pct'] * 100 * 0.3) +
        (base['sl'] * 1.5) +
        matchup_bias +
        trend +
        consistency +
        (race_prof['race_eff'] * 10) +
        (hill_rate * 8) -
        (race_prof['collapse_losses'] * 0.5)
    )
    return {
        'rating': rating,
        'base': base,
        'matches': matches,
        'matchup_bias': matchup_bias,
        'trend': trend,
        'consistency': consistency,
        'race': race_prof,
        'hill_rate': hill_rate
    }

def team_rating_enhanced(team_id: str, fmt: str, league: dict, opp_team_id: str = None):
    teams = league.get('teams', {}) or {}
    players = league.get('players', {}) or {}
    team = teams.get(team_id, {})
    opp_team = teams.get(opp_team_id, {}) if opp_team_id else {}
    roster_ids = team_roster_ids(team)
    opp_sls = []
    for pid in team_roster_ids(opp_team):
        opp = players.get(pid) or players.get(str(pid), {})
        sl = (opp.get('current_skill_levels') or {}).get(fmt, 0)
        opp_sls.append(sl)
    ratings = []
    race_effs = []
    hill_rates = []
    for pid in roster_ids:
        pr = player_rating_enhanced(pid, fmt, league, opp_sls)
        ratings.append(pr)
        race_effs.append(pr.get('race', {}).get('race_eff', 0))
        hill_rates.append(pr.get('hill_rate', 0))
    if not ratings:
        return {'rating': 0, 'components': {}}
    avg_rating = sum(r['rating'] for r in ratings) / len(ratings)
    avg_trend = sum(r['trend'] for r in ratings) / len(ratings)
    avg_consistency = sum(r['consistency'] for r in ratings) / len(ratings)
    avg_race_eff = sum(race_effs) / len(race_effs) if race_effs else 0
    avg_hill = sum(hill_rates) / len(hill_rates) if hill_rates else 0
    comp = {
        'player_count': len(ratings),
        'avg_rating': avg_rating,
        'trend': avg_trend,
        'consistency': avg_consistency,
        'race_eff': avg_race_eff,
        'hill_conf': avg_hill
    }
    return {'rating': avg_rating + avg_trend + avg_consistency + (avg_race_eff*5) + (avg_hill*6), 'components': comp}

def predict_match(div_id: str, fmt: str, our_team: str, opp_team: str, league: dict):
    """Lightweight matchup prediction based on roster PPM/PA."""
    def team_name(tid):
        return pretty_team_name(tid, league.get('teams', {}).get(tid, {}).get('name', tid))

    ours = team_rating_enhanced(our_team, fmt, league, opp_team)
    theirs = team_rating_enhanced(opp_team, fmt, league, our_team)
    r1, r2 = ours['rating'], theirs['rating']

    # Projection scaling using race expectations
    if fmt == '9-ball':
        score_us = max(35, min(75, r1 * 1.1))
        score_them = max(35, min(75, r2 * 1.1))
    else:
        score_us = max(8, min(25, r1 * 0.35))
        score_them = max(8, min(25, r2 * 0.35))

    diff = r1 - r2
    if diff > 5:
        fav = 'Cue-ligans clear favorite'
    elif diff > 1:
        fav = 'Cue-ligans slight favorite'
    elif diff < -5:
        fav = f"{team_name(opp_team)} clear favorite"
    elif diff < -1:
        fav = f"{team_name(opp_team)} slight favorite"
    else:
        fav = 'Too close to call'

    reasons = []
    if ours['components'].get('trend', 0) > theirs['components'].get('trend', 0):
        reasons.append('Recent form stronger')
    if ours['components'].get('consistency', 0) > theirs['components'].get('consistency', 0):
        reasons.append('More consistent scoring')
    if diff > 0:
        reasons.append('Higher composite rating vs opponent')
    if ours['components'].get('race_eff', 0) > theirs['components'].get('race_eff', 0):
        reasons.append('Race-based advantage')

    return {
        'division': div_id,
        'format': fmt,
        'our_team': team_name(our_team),
        'opp_team': team_name(opp_team),
        'our_rating': round(r1, 2),
        'their_rating': round(r2, 2),
        'proj_us': round(score_us),
        'proj_them': round(score_them),
        'summary': fav,
        'ours': ours,
        'theirs': theirs,
        'reasons': reasons
    }

# Reusable LAB helpers
def get_roster_for_team(team_id: str, fmt: str, league: dict):
    teams = league.get('teams', {}) or {}
    players = league.get('players', {}) or {}
    team = teams.get(team_id, {})
    roster_ids = team_roster_ids(team)
    roster = []
    for pid in roster_ids:
        p = players.get(pid) or players.get(str(pid), {})
        if not p:
            continue
        base = player_metrics(p, fmt)
        # Skip players without a valid SL for this format
        try:
            if not base.get('sl') or base.get('sl', 0) <= 0:
                continue
        except Exception:
            continue
        race = player_race_profile(pid, fmt, league)
        role = None
        if race.get('hill_wins', 0) >= 3 and (race.get('hill_attempts', 0) or 0) > 0:
            if (race['hill_wins'] / max(1, race['hill_attempts'])) >= 0.6:
                role = 'Closer'
        if base.get('win_pct', 0) > 0.65 and (base.get('ppm', 0) or 0) > 10:
            role = role or 'Starter'
        roster.append({
            'id': pid,
            'name': p.get('full_name', pid),
            'sl': base['sl'],
            'ppm': base['ppm'],
            'pa': base['pa'],
            'win_pct': base['win_pct'],
            'role': role or 'Contributor'
        })
    roster.sort(key=lambda r: (-r['ppm'], -r['pa'], r['sl']))
    return roster


def compute_patch_counts(player_id: str, league: dict):
    """Derive simple APA patch counts from this season's match/sets data."""
    matches = league.get('matches', {}) or {}
    counts = {}

    def inc(code):
        counts[code] = counts.get(code, 0) + 1

    for m in matches.values():
        fmt = m.get('format')
        if fmt not in ('8-ball', '9-ball'):
            continue
        for s in m.get('sets', []) or []:
            pid_home = s.get('home_player_id')
            pid_away = s.get('away_player_id')
            if player_id not in (pid_home, pid_away):
                continue
            pf = s.get('home_points') if pid_home == player_id else s.get('away_points')
            pa = s.get('away_points') if pid_home == player_id else s.get('home_points')
            try:
                pf = float(pf or 0)
                pa = float(pa or 0)
            except Exception:
                pf = pf or 0
                pa = pa or 0
            # Match outcome patches
            if pf > pa:
                inc('match_win')
            if pa == 0 and pf > 0:
                inc('shutout')
                if fmt == '8-ball':
                    inc('rackless')
            if abs(pf - pa) <= 1 and max(pf, pa) > 0:
                inc('hill_hill')
            # Very rough high-performance proxy
            if pf >= 3 and fmt == '8-ball':
                inc('high_win_margin')
            if pf >= 20 and fmt == '9-ball':
                inc('high_win_margin')

    return counts

def run_simulation(div_id: str, sims: int, league: dict, fmt: str, remaining_weeks: int = 4, variance: float = 0.1):
    divisions = league.get('divisions', {})
    standings_rows = divisions.get(div_id, {}).get('standings', [])
    current_points = {}
    for row in standings_rows:
        tid = row.get('team_id')
        if not tid:
            continue
        try:
            pts = float(row.get('points') or 0)
        except Exception:
            pts = 0.0
        current_points[tid] = pts
    expected = {}
    for row in standings_rows:
        tid = row.get('team_id')
        metrics = team_rating_enhanced(tid, fmt, league)
        base = metrics.get('rating') if isinstance(metrics, dict) else 50
        try:
            base = float(base)
        except Exception:
            base = 50.0
        if fmt == '9-ball':
            exp = max(30, min(80, base * 1.0))
        else:
            exp = max(8, min(25, base * 0.35))
        expected[tid] = exp
    top_counts = {tid: {'first': 0, 'top3': 0} for tid in current_points.keys()}
    team_list = list(current_points.keys())
    for _ in range(sims):
        totals = {}
        for tid in team_list:
            try:
                mean = float(expected.get(tid, 50) or 50)
            except Exception:
                mean = 50.0
            total = current_points.get(tid, 0) or 0
            for _w in range(remaining_weeks):
                total += max(0, random.gauss(mean, mean * variance))
            totals[tid] = total
        ranked = sorted(team_list, key=lambda x: totals.get(x, 0), reverse=True)
        for idx, tid in enumerate(ranked):
            if idx == 0:
                top_counts[tid]['first'] += 1
            if idx < 3:
                top_counts[tid]['top3'] += 1
    rows = []
    for tid in team_list:
        rows.append({
            'team': pretty_team_name(tid, league.get('teams', {}).get(tid, {}).get('name', tid)),
            'current': current_points.get(tid, 0),
            'pct_first': round(100 * top_counts[tid]['first'] / sims, 1),
            'pct_top3': round(100 * top_counts[tid]['top3'] / sims, 1),
            'cue': normalize_team_name(pretty_team_name(tid, league.get('teams', {}).get(tid, {}).get('name', tid))) == MY_TEAM_KEY
        })
    rows.sort(key=lambda r: r['pct_first'], reverse=True)
    return rows

def generate_lineup(fmt: str, team_id: str, sl_cap: int, league: dict):
    return generate_lineup_advanced(fmt, team_id, sl_cap, league)

def generate_lineup_advanced(fmt: str, team_id: str, sl_cap: int, league: dict, includes=None, locks=None, strategy='balanced', opp_team_id=None, sl_min=None, sl_max=None, min_low_sl=0, require_high_sl=False):
    includes = set(includes or [])
    locks = set(locks or [])
    roster = get_roster_for_team(team_id, fmt, league)
    if includes:
        roster = [r for r in roster if r['id'] in includes]
    roster_ids = [r['id'] for r in roster]
    if locks:
        roster = [r for r in roster if r['id'] in includes or r['id'] in locks or not includes] if includes else roster
    players_map = {r['id']: r for r in roster}
    if len(roster) < 5:
        return []
    # Opponent SL distribution for bias
    opp_sls = []
    if opp_team_id:
        opp_roster = get_roster_for_team(opp_team_id, fmt, league)
        opp_sls = [p['sl'] for p in opp_roster]
    def strategy_score(ppm_avg, pa_avg, sl_total, combo_players):
        sl_factor = sl_total / len(combo_players)
        if strategy == 'max_points':
            return (ppm_avg * 2.2) + (pa_avg * 8) + (sl_factor * 0.5)
        elif strategy == 'upset':
            # Boost players with higher SL differential potential
            boost = 0
            for p in combo_players:
                if p['sl'] <= 4 and ppm_avg > 8:
                    boost += 1
            return (ppm_avg * 1.6) + (pa_avg * 10) + boost
        else:  # balanced
            return ppm_avg + (pa_avg * 10) + sl_factor
    best = []
    roster_list = roster
    for combo in itertools.combinations(roster_list, 5):
        sl_total = sum(c['sl'] for c in combo)
        if sl_total > sl_cap:
            continue
        if locks and not locks.issubset({c['id'] for c in combo}):
            continue
        if sl_min and sl_total < sl_min:
            continue
        if sl_max and sl_total > sl_max:
            continue
        low_count = sum(1 for c in combo if c['sl'] <= 4)
        high_count = sum(1 for c in combo if c['sl'] >= 6)
        if min_low_sl and low_count < min_low_sl:
            continue
        if require_high_sl and high_count < 1:
            continue
        ppm_avg = sum(c['ppm'] for c in combo) / len(combo)
        pa_avg = sum(c['pa'] for c in combo) / len(combo)
        score = strategy_score(ppm_avg, pa_avg, sl_total, combo)
        # Opponent-aware bias
        if opp_sls:
            opp_median = sorted(opp_sls)[len(opp_sls)//2]
            match_bonus = sum(0.2 for c in combo if abs(c['sl'] - opp_median) <= 1)
            score += match_bonus
        best.append({'players': combo, 'sl_total': sl_total, 'score': score, 'ppm_avg': ppm_avg, 'pa_avg': pa_avg})
    best.sort(key=lambda x: x['score'], reverse=True)
    return best

def player_trend(pid: str, fmt: str, league: dict, cue_team: str = None):
    players = league.get('players', {}) or {}
    teams = league.get('teams', {}) or {}
    player = players.get(pid) or players.get(str(pid), {})
    m = player_metrics(player, fmt)
    cue_roster = team_roster_ids(teams.get(cue_team, {})) if cue_team else []
    team_metrics = [player_metrics(players.get(rid, {}), fmt) for rid in cue_roster if players.get(rid)]
    team_ppm = sum(tm['ppm'] for tm in team_metrics) / len(team_metrics) if team_metrics else 0
    diff = m['ppm'] - team_ppm
    trend_text = 'Stable'
    if diff > 0.5:
        trend_text = 'Trending up'
    elif diff < -0.5:
        trend_text = 'Trending down'
    return {
        'player': player.get('full_name', pid),
        'format': fmt,
        'sl': m['sl'],
        'win_pct': round(m['win_pct'] * 100, 1),
        'ppm': round(m['ppm'], 2),
        'pa': round(m['pa'] * 100, 1),
        'team_ppm': round(team_ppm, 2),
        'diff': round(diff, 2),
        'trend': trend_text,
    }

def win_probability(fmt: str, our_pid: str, opp_pid: str, league: dict):
    players = league.get('players', {}) or {}

    def player_profile(pid):
        p = players.get(pid, {})
        base = player_metrics(p, fmt)
        matches = player_match_analytics(pid, fmt, league)
        race = player_race_profile(pid, fmt, league)
        return {'base': base, 'matches': matches, 'race': race, 'name': p.get('full_name', pid)}

    def band_perf(pid, opp_sl):
        # Single-band performance around a target SL
        all_matches = player_matches(pid, fmt, league)
        if not all_matches:
            return 0, 0
        target = [m for m in all_matches if abs(m['opp_sl'] - opp_sl) <= 0.5]
        if not target:
            target = all_matches
        win_pct = sum(1 for m in target if m['outcome'] == 'W') / len(target)
        ppm = sum(m['points_for'] for m in target) / len(target)
        return win_pct, ppm

    def compute_rating(pid, opp_sl=None):
        prof = player_profile(pid)
        b = prof['base']
        m = prof['matches']
        r = prof['race']
        hill = (r.get('hill_wins', 0) / r.get('hill_attempts', 1)) if r.get('hill_attempts') else 0
        vs_band = 0
        if opp_sl is not None:
            band_win, band_ppm = band_perf(pid, opp_sl)
            vs_band = (band_win * 10) + band_ppm
        recent = m.get('recent_avg', b['ppm'])
        rating = (
            (b['sl'] * 10) +
            (b['ppm'] * 1.5) +
            (b['pa'] * 100 * 0.4) +
            (b['win_pct'] * 100 * 0.3) +
            (recent * 0.5) +
            (vs_band * 0.5) +
            (hill * 5)
        )
        return rating, prof

    # Opponent SL for band matching
    opp_player = players.get(opp_pid, {})
    opp_sl = (opp_player.get('current_skill_levels') or {}).get(fmt, 0)
    r1, prof1 = compute_rating(our_pid, opp_sl)
    r2, prof2 = compute_rating(opp_pid, prof1['base']['sl'])
    prob = r1 / (r1 + r2 + 1e-6)
    prob = max(0.1, min(0.9, prob))
    def simplify(prof):
        b = prof['base']
        return {
            'name': prof['name'],
            'sl': b['sl'],
            'win_pct': b['win_pct'],
            'ppm': b['ppm'],
            'pa': b['pa'],
            'recent_ppm': prof['matches'].get('recent_avg', b['ppm']),
            'hill': (prof['race'].get('hill_wins', 0) / prof['race'].get('hill_attempts', 1)) if prof['race'].get('hill_attempts') else 0,
            'vs_high': {'win_pct': prof['matches'].get('wr_high', 0), 'ppm': prof['matches'].get('avg_high', 0)},
            'vs_equal': {'win_pct': prof['matches'].get('wr_equal', 0), 'ppm': prof['matches'].get('avg_equal', 0)},
            'vs_low': {'win_pct': prof['matches'].get('wr_low', 0), 'ppm': prof['matches'].get('avg_low', 0)},
        }
    notes = []
    if prof1['matches'].get('recent_avg', 0) > prof2['matches'].get('recent_avg', 0):
        notes.append('Recent momentum favors our player.')
    if prof1['matches'].get('wr_high', 0) > 0.5 and opp_sl >= prof1['base']['sl']:
        notes.append('Performs well vs higher SL opponents.')
    if prof1['race'].get('hill_wins', 0) and prof1['race'].get('hill_attempts', 0):
        hill_rate = prof1['race']['hill_wins']/prof1['race']['hill_attempts']
        if hill_rate > 0.5:
            notes.append('Strong hill/hill closer.')

    return {
        'format': fmt,
        'our_name': prof1['name'],
        'their_name': prof2['name'],
        'prob': round(prob * 100, 1),
        'ratings': {'ours': round(r1,2), 'theirs': round(r2,2)},
        'ours': simplify(prof1),
        'theirs': simplify(prof2),
        'notes': notes
    }

def race_calculation(fmt: str, pid_a: str, pid_b: str, league: dict):
    players = league.get('players', {}) or {}

    def profile(pid):
        p = players.get(pid, {})
        base = player_metrics(p, fmt)
        matches = player_match_analytics(pid, fmt, league)
        race_prof = player_race_profile(pid, fmt, league)
        return {
            'id': pid,
            'name': p.get('full_name', pid),
            'base': base,
            'matches': matches,
            'race': race_prof,
            'sl': base['sl'],
            'win_pct': base['win_pct'],
            'ppm': base['ppm'],
            'pa': base['pa'],
        }

    prof_a = profile(pid_a)
    prof_b = profile(pid_b)
    sl_a, sl_b = prof_a['sl'], prof_b['sl']
    race_a = race_length(fmt, sl_a)
    race_b = race_length(fmt, sl_b)

    # Ratio tension: tighter race lengths and closer scoring raise hill chances
    diff_sl = abs(sl_a - sl_b)
    diff_ppm = abs(prof_a['ppm'] - prof_b['ppm'])
    race_ratio = min(race_a, race_b) / max(race_a, race_b) if max(race_a, race_b) else 0
    balance = 1 - min(1, abs((prof_a['ppm'] / max(race_a, 1)) - (prof_b['ppm'] / max(race_b, 1))))
    hill_prob = 0.2 + (race_ratio * 0.3) + (balance * 0.3) - (diff_sl * 0.02) - (diff_ppm * 0.01)
    hill_prob = max(0.05, min(0.7, hill_prob))

    def hill_chance(p):
        # More weight to PA% (conversion) and win%
        return max(0.1, min(0.95, (p['win_pct'] * 0.55) + (p['pa'] * 0.35) + 0.12))

    a_hill = hill_chance(prof_a)
    b_hill = hill_chance(prof_b)

    innings = max(4, int((race_a + race_b) * 0.6)) if fmt.startswith('8') else None
    points_flow = None
    if fmt.startswith('9'):
        denom = (prof_a['ppm'] + prof_b['ppm'] + 1e-6)
        points_flow = {
            'a_share': round((prof_a['ppm'] / denom) * 100, 1),
            'b_share': round((prof_b['ppm'] / denom) * 100, 1),
        }

    def band_perf(pid, target_sl):
        matches = player_matches(pid, fmt, league)
        lower = [m for m in matches if m['opp_sl'] < target_sl]
        equal = [m for m in matches if abs(m['opp_sl'] - target_sl) <= 0.5]
        higher = [m for m in matches if m['opp_sl'] > target_sl]
        def agg(lst):
            if not lst:
                return {'win_pct': 0, 'ppm': 0, 'count': 0}
            return {
                'win_pct': sum(1 for m in lst if m['outcome'] == 'W')/len(lst),
                'ppm': sum(m['points_for'] for m in lst)/len(lst),
                'count': len(lst)
            }
        return {'lower': agg(lower), 'equal': agg(equal), 'higher': agg(higher)}

    band_a = band_perf(prof_a['id'], sl_b)
    band_b = band_perf(prof_b['id'], sl_a)

    notes = []
    if prof_a['win_pct'] > prof_b['win_pct']:
        notes.append(f"{prof_a['name']} has stronger win% this session.")
    if prof_b['ppm'] > prof_a['ppm']:
        notes.append(f"{prof_b['name']} scores more points per match on average.")
    if band_a['higher']['win_pct'] > 0.5 and sl_b > sl_a:
        notes.append(f"{prof_a['name']} overperforms vs higher SL opponents.")
    if band_b['lower']['win_pct'] < 0.4 and sl_a < sl_b:
        notes.append(f"{prof_b['name']} can stumble vs lower SL opponents.")

    # Scoring notes for UI reference (APA specific)
    scoring_notes = {
        '9-ball': ("Points race: each ball = 1 point, 9-ball = 2. Higher SL must "
                   "outpace low-SL by ratio of race lengths; steady scoring pressure from low SL can force hill-hill."),
        '8-ball': ("Team points: sweep 3-0 awards 3 points; win with opponent on hill yields 2-1; "
                   "if opponent never reaches hill, winner gets 2, loser 0. Hill is one rack from victory.")
    }

    return {
        'format': fmt,
        'a': {
            'name': prof_a['name'],
            'sl': sl_a,
            'race': race_a,
            'win_pct': prof_a['win_pct'],
            'ppm': prof_a['ppm'],
            'pa': prof_a['pa'],
            'band': band_a,
        },
        'b': {
            'name': prof_b['name'],
            'sl': sl_b,
            'race': race_b,
            'win_pct': prof_b['win_pct'],
            'ppm': prof_b['ppm'],
            'pa': prof_b['pa'],
            'band': band_b,
        },
        'hill_prob': round(hill_prob * 100, 1),
        'dynamics': {
            'a_hill': round(a_hill * 100, 1),
            'b_hill': round(b_hill * 100, 1),
            'innings': innings,
            'points_flow': points_flow,
        },
        'notes': notes,
        'rules': scoring_notes.get(fmt, '')
    }

def find_cue_team_ids(league):
    teams = league.get('teams', {})
    divisions = league.get('divisions', {})
    cue_8 = cue_9 = None
    for tid, t in teams.items():
        name = normalize_team_name(t.get('name', ''))
        fmt = t.get('format')
        if not fmt:
            div = divisions.get(t.get('division_id') or '')
            fmt = (div or {}).get('type') or (div or {}).get('format') or ''
        fmt = fmt or ''
        if name == MY_TEAM_KEY:
            if not fmt:
                # Fallback: if format missing, assume single team plays both
                cue_8 = cue_8 or tid
                cue_9 = cue_9 or tid
            else:
                if '8-ball' in fmt and not cue_8:
                    cue_8 = tid
                if '9-ball' in fmt and not cue_9:
                    cue_9 = tid
    return cue_8, cue_9
def get_standings_from_league():
    data = load_league_data()
    return build_standings_from_league(data)

def get_rosters_from_league():
    data = load_league_data()
    return build_rosters_from_league(data)

# --- Team Scouting Route ---
@app.route('/scouting')
def scouting():
    data = load_league_data()
    divisions = data.get('divisions', {}) or {}
    teams = data.get('teams', {}) or {}
    players = data.get('players', {}) or {}
    session_label = data.get('meta', {}).get('session', 'Current Session')

    # Build division lookup and filter list
    division_lookup = {}
    division_filters = []
    for div_id, div in divisions.items():
        division_lookup[str(div_id)] = div
        division_filters.append({
            "id": str(div_id),
            "name": div.get("name"),
            "format": div.get("format"),
            "code": div.get("apa_division_id") or div.get("code"),
            "night": div.get("night"),
            "session": div.get("session"),
        })

    def team_rank_info(div, team_id):
        standings = (div or {}).get('standings') or []
        for s in standings:
            if s.get('team_id') == team_id:
                return s.get('rank'), s.get('points'), s.get('points_last_week'), s
        return None, None, None, {}

    def avg_skill(roster_ids, fmt):
        vals = []
        for pid in roster_ids:
            p = players.get(str(pid)) or players.get(pid) or {}
            sl = (p.get('current_skill_levels') or {}).get(fmt, 0)
            if sl:
                vals.append(sl)
        return round(sum(vals) / len(vals), 2) if vals else 0

    def avg_stat(roster_ids, fmt, key):
        vals = []
        for pid in roster_ids:
            p = players.get(str(pid)) or players.get(pid) or {}
            stats = (p.get('stats') or {}).get(fmt) or {}
            val = stats.get(key)
            if val is None:
                continue
            if key in ('win_pct', 'pa') and val > 1:
                val = val / 100
            vals.append(val)
        return round(sum(vals) / len(vals), 3) if vals else 0

    team_rows = []
    for team_id, team in teams.items():
        div_id = str(team.get('division_id'))
        div = division_lookup.get(div_id, {})
        rank, points, last_week, standings_row = team_rank_info(div, team_id)
        session_summary = team.get('session_summary') or {}
        roster_raw = team.get('roster') or team.get('roster_alias_ids') or team.get('player_ids') or []
        roster_ids = []
        for ref in roster_raw:
            if isinstance(ref, dict):
                rid = ref.get('player_id') or ref.get('id')
            else:
                rid = ref
            if rid:
                roster_ids.append(rid)
        fmt = team.get('format') or div.get('format')
        avg_sl = avg_skill(roster_ids, '8-ball' if fmt and fmt.startswith('8') else '9-ball')
        avg_ppm = session_summary.get('ppm') or avg_stat(roster_ids, fmt, 'ppm')
        avg_pa = session_summary.get('pa') or avg_stat(roster_ids, fmt, 'pa')
        avg_win = session_summary.get('win_pct') or avg_stat(roster_ids, fmt, 'win_pct')
        tags = []
        if rank:
            if rank <= 2:
                tags.append("Top of Division")
            elif rank <= 4:
                tags.append("Contender")
            elif rank >= 7:
                tags.append("Bottom of Division")
        if avg_ppm and avg_ppm >= 12:
            tags.append("High-PPM Offense")
        if avg_pa and avg_pa >= 0.6:
            tags.append("High PA")
        team_rows.append({
            "team_id": team_id,
            "display_name": team.get('name') or team_id,
            "format": fmt or '-',
            "division_name": div.get('name') or '-',
            "division_code": div.get('apa_division_id') or div.get('code'),
            "session": div.get('session') or session_label,
            "rank": rank or 0,
            "session_total_points": session_summary.get('session_total_points') or points or 0,
            "points_last_week": session_summary.get('points_last_week') or last_week or 0,
            "total_team_matches_played": session_summary.get('total_team_matches_played') or standings_row.get('matches_played') or 0,
            "home_location": team.get('home_location'),
            "roster_size": len(roster_ids),
            "avg_sl": avg_sl,
            "avg_ppm": avg_ppm,
            "avg_pa": avg_pa,
            "avg_win": avg_win,
            "tags": tags,
        })

    # Filters
    q_div = request.args.get('division') or ''
    q_fmt = request.args.get('format') or ''
    q_search = (request.args.get('search') or '').lower()
    q_tag = request.args.get('tag') or ''
    sort = request.args.get('sort') or 'rank'
    direction = request.args.get('dir') or 'asc'

    def match_filters(row):
        if q_div and str(division_lookup.get(q_div, {}).get('name')) != row['division_name'] and q_div != row.get('division_code'):
            if str(row.get('division_name')) != q_div and str(row.get('division_code')) != q_div and str(q_div) != str(row.get('division_name')):
                return False
        if q_fmt and q_fmt.lower() != 'all' and q_fmt.lower() != str(row.get('format', '')).lower():
            return False
        if q_search and q_search not in row.get('display_name', '').lower():
            return False
        if q_tag and q_tag not in row.get('tags', []):
            return False
        return True

    team_rows = [r for r in team_rows if match_filters(r)]

    sort_key = {
        "rank": lambda r: r.get('rank') or 99,
        "points": lambda r: r.get('session_total_points') or 0,
        "ppm": lambda r: r.get('avg_ppm') or 0,
        "pa": lambda r: r.get('avg_pa') or 0,
        "win": lambda r: r.get('avg_win') or 0,
    }.get(sort, lambda r: r.get('rank') or 99)
    reverse = direction == 'desc'
    team_rows = sorted(team_rows, key=sort_key, reverse=reverse)

    summary_cards = {
        "total_teams": len(team_rows),
        "divisions": len(division_filters),
        "top_offense": max(team_rows, key=lambda r: r.get('avg_ppm') or 0)['display_name'] if team_rows else '-',
        "points_spread": 0
    }
    # points spread
    pts = [r.get('session_total_points') or 0 for r in team_rows]
    if pts:
        summary_cards["points_spread"] = max(pts) - min(pts)

    tag_options = ["Top of Division", "Contender", "Bottom of Division", "High-PPM Offense", "High PA"]

    return render_template(
        'scouting.html',
        teams=team_rows,
        divisions=division_filters,
        formats=['all', '8-ball', '9-ball'],
        session_label=session_label,
        tag_options=tag_options,
        summary_cards=summary_cards,
        current_filters={
            "division": q_div,
            "format": q_fmt,
            "search": request.args.get('search') or '',
            "tag": q_tag,
            "sort": sort,
            "dir": direction
        }
    )
# --- Robust Dashboard Route (user drop-in) ---
@app.route('/')
def dashboard():
    user = get_current_user()
    league_data = load_league_data()
    league_teams = (league_data.get('teams') or {}) if isinstance(league_data, dict) else {}
    target_team_id = user.team_id if user else None
    target_team_name = 'Cue-Ligans'
    target_team_key = MY_TEAM_KEY
    if target_team_id and target_team_id in league_teams:
        team_entry = league_teams[target_team_id] or {}
        display = pretty_team_name(target_team_id, team_entry.get('name') or target_team_id)
        target_team_name = display
        target_team_key = normalize_team_name(display)
    # Robustly load standings data
    try:
        standings_data = get_standings_from_league()
        if not isinstance(standings_data, dict):
            standings_data = {}
    except Exception as e:
        logging.error(f"Error loading league data for dashboard: {e}")
        standings_data = {}
    # Chart data for 8-ball and 9-ball
    standings_8_chart = {
        'labels': [t.get('team', '') for t in standings_data.get('eight_ball', [])],
        'points': [t.get('points', 0) for t in standings_data.get('eight_ball', [])]
    }
    standings_9_chart = {
        'labels': [t.get('team', '') for t in standings_data.get('nine_ball', [])],
        'points': [t.get('points', 0) for t in standings_data.get('nine_ball', [])]
    }
    # Unified labels so the comparison chart can align 8/9-ball points
    combined_labels = []
    label_set = set()
    for lbl in standings_8_chart['labels'] + standings_9_chart['labels']:
        if lbl not in label_set:
            label_set.add(lbl)
            combined_labels.append(lbl)
    points_map_8 = dict(zip(standings_8_chart['labels'], standings_8_chart['points']))
    points_map_9 = dict(zip(standings_9_chart['labels'], standings_9_chart['points']))
    points_combo = {
        'labels': combined_labels,
        'eight': [points_map_8.get(lbl, 0) for lbl in combined_labels],
        'nine': [points_map_9.get(lbl, 0) for lbl in combined_labels],
    }
    # Standings for Cue-ligans
    standings = {"8ball": {"rank": 0, "points": 0}, "9ball": {"rank": 0, "points": 0}}
    try:
        # 8-ball
        for idx, team in enumerate(standings_data.get('eight_ball', []), 1):
            if normalize_team_name(team.get('team', '')) == target_team_key:
                standings['8ball']['rank'] = team.get('rank', idx)
                standings['8ball']['points'] = int(team.get('points', 0))
                break
        # 9-ball
        for idx, team in enumerate(standings_data.get('nine_ball', []), 1):
            if normalize_team_name(team.get('team', '')) == target_team_key:
                standings['9ball']['rank'] = team.get('rank', idx)
                standings['9ball']['points'] = int(team.get('points', 0))
                break
    except Exception as e:
        logging.error(f"Error extracting standings for dashboard: {e}")
    # Dues
    dues = {"players": {}}
    total_owed = 0
    dues_table = []
    collected = 0
    target = 0
    try:
        dues = load_json('dues.json')
        if isinstance(dues, dict) and 'players' in dues and isinstance(dues['players'], dict):
            for name, pd in dues['players'].items():
                paid8 = pd.get('paid8', False)
                paid9 = pd.get('paid9', False)
                owes = 0
                if not paid8:
                    owes += 9
                else:
                    collected += 9
                if not paid9:
                    owes += 9
                else:
                    collected += 9
                dues_table.append({"name": name, "paid8": paid8, "paid9": paid9, "owes": owes})
                total_owed += owes
            num_players = len(dues['players'])
            target = num_players * 9 * 2
        else:
            target = 1
    except Exception as e:
        logging.error(f"Error loading dues for dashboard: {e}")
    if (
        isinstance(dues, dict)
        and not isinstance(dues, list)
        and 'players' in dues
        and isinstance(dues['players'], dict)
    ):
        # Only assign if dues is a dict with string keys (ignore mypy false positive)
        dues.__setitem__('collected', collected)  # type: ignore
        dues.__setitem__('target', target)  # type: ignore
    # Schedule
    schedule_next = []
    from datetime import datetime
    import pytz
    tz = pytz.timezone('America/Chicago')  # Texas time
    today = datetime.now(tz).date()
    days_until_next_game = None
    try:
        schedule = load_json('schedule.json')
        for week in schedule:
            for match in week.get('matches', []):
                home_team = normalize_team_name(match.get('home', ''))
                away_team = normalize_team_name(match.get('away', ''))
                if home_team == target_team_key or away_team == target_team_key:
                    if home_team == target_team_key:
                        opponent = match.get('away', '')
                        location = "DK's"
                    else:
                        opponent = match.get('home', '')
                        location = "Lucky Barrel"
                    schedule_next.append({
                        "week": week.get("week", 0),
                        "opponent": opponent,
                        "format": "Both",
                        "location": location,
                        "date": week.get("date", "")
                    })
        # Find the next game date (future match with the soonest date)
        future_matches = [m for m in schedule_next if m['date'] >= str(today)]
        schedule_next = future_matches[:4] if future_matches else schedule_next[-4:]
        if future_matches:
            from datetime import date as dt_date
            # Parse the soonest date
            try:
                next_game_date = min(dt_date.fromisoformat(m['date']) for m in future_matches if m['date'])
                days_until_next_game = (next_game_date - today).days
            except Exception as e:
                logging.warning(f"Could not parse next game date: {e}")
    except Exception as e:
        logging.error(f"Error loading schedule for dashboard: {e}")
    # Recent Results
    recent_results = []
    try:
        played_matches = [m for m in schedule_next if m['date'] < str(today)]
        recent_results = played_matches[-4:]
    except Exception:
        pass
    # Roster stats
    leaderboard_8 = []
    leaderboard_9 = []
    try:
        rosters = get_rosters_from_league()
        cue_ligans = None
        if isinstance(rosters, list):
            for team in rosters:
                if normalize_team_name(team.get('team', '')) == MY_TEAM_KEY:
                    cue_ligans = team
                    break
        def add_matches_field(player):
            # Add 'matches' field as string x/x if present, else empty string
            matches = player.get('matches', '')
            if isinstance(matches, str):
                player['games_played'] = matches
            elif isinstance(matches, (list, tuple)) and len(matches) == 2:
                player['games_played'] = f"{matches[0]}/{matches[1]}"
            else:
                player['games_played'] = str(matches)
            return player
        if cue_ligans:
            leaderboard_8 = cue_ligans.get('eight_ball', [])
            for p in leaderboard_8:
                p['sl'] = p.get('skill_level', '')
                try:
                    p['win_pct'] = float(str(p.get('win_pct', '0')).replace('%',''))
                except Exception:
                    p['win_pct'] = 0.0
                try:
                    p['ppm'] = float(p.get('ppm', 0))
                except Exception:
                    p['ppm'] = 0.0
                try:
                    p['pa'] = float(str(p.get('pa', '0')).replace('%',''))
                except Exception:
                    p['pa'] = 0.0
                add_matches_field(p)
            leaderboard_8.sort(key=lambda p: (-(p['win_pct']), -(p['ppm']), -(p['pa'])))
            leaderboard_9 = cue_ligans.get('nine_ball', [])
            for p in leaderboard_9:
                p['sl'] = p.get('skill_level', '')
                try:
                    p['win_pct'] = float(str(p.get('win_pct', '0')).replace('%',''))
                except Exception:
                    p['win_pct'] = 0.0
                try:
                    p['ppm'] = float(p.get('ppm', 0))
                except Exception:
                    p['ppm'] = 0.0
                try:
                    p['pa'] = float(str(p.get('pa', '0')).replace('%',''))
                except Exception:
                    p['pa'] = 0.0
                add_matches_field(p)
            leaderboard_9.sort(key=lambda p: (-(p['win_pct']), -(p['ppm']), -(p['pa'])))
        else:
            logging.warning('Cue-ligans roster not found in league data')
    except Exception as e:
        logging.error(f"Error loading roster stats from league data: {e}")
    # Chart and trend data
    chart_data = {
        "labels": ["8-Ball", "9-Ball"],
        "points": [standings["8ball"]["points"], standings["9ball"]["points"]]
    }

    # Trend and weekly performance
    trend_labels = []
    trend_eight = []
    trend_nine = []
    trend_target8 = []
    trend_target9 = []
    current_week = 0
    weekly_scores_8 = []
    weekly_scores_9 = []
    weekly_count = 5
    trend_team_base = request.args.get('trend_team', 'Cue-ligans')
    all_team_ids = []
    team_pairs = {}
    weekly_all = {}
    try:
        weekly_count = int(request.args.get('weekly_count', 5))
    except Exception:
        weekly_count = 5
    try:
        from datetime import date as dt_date
        schedule = load_json('schedule.json')
        date_to_week = {}
        for wk in schedule:
            if isinstance(wk, dict) and wk.get('date'):
                date_to_week[wk['date']] = wk.get('week', 0)

        league = load_league_data()
        matches = league.get('matches', {})
        teams = league.get('teams', {})
        all_team_ids = list(teams.keys())

        # Build mapping of pretty team name -> ids for 8/9 formats
        for tid, t in teams.items():
            base_name = pretty_team_name(tid, t.get('name', tid))
            entry = team_pairs.setdefault(base_name, {})
            fmt = t.get('format', '').lower()
            if '8-ball' in fmt:
                entry['eight'] = tid
            elif '9-ball' in fmt:
                entry['nine'] = tid
        if trend_team_base not in team_pairs:
            trend_team_base = pretty_team_name('cue_ligans_8', 'Cue-ligans')

        trend_team_8 = team_pairs.get(trend_team_base, {}).get('eight', 'cue_ligans_8')
        trend_team_9 = team_pairs.get(trend_team_base, {}).get('nine', 'cue_ligans_9')

        def build_weekly_for(team_id):
            weekly_totals = {}
            weekly_details = {}
            for m in matches.values():
                if m.get('home_team_id') == team_id:
                    pts = m.get('team_scores', {}).get('home_total', 0)
                    opp_id = m.get('away_team_id')
                    opp_pts = m.get('team_scores', {}).get('away_total', 0)
                elif m.get('away_team_id') == team_id:
                    pts = m.get('team_scores', {}).get('away_total', 0)
                    opp_id = m.get('home_team_id')
                    opp_pts = m.get('team_scores', {}).get('home_total', 0)
                else:
                    continue
                date_str = m.get('date')
                try:
                    wk_date = dt_date.fromisoformat(date_str)
                except Exception:
                    continue
                if wk_date > today:
                    continue
                week_num = date_to_week.get(date_str, m.get('week', 0))
                if not week_num:
                    try:
                        week_num = int(m.get('week_number', 0))
                    except Exception:
                        week_num = 0
                if not week_num:
                    continue
                opp_name = pretty_team_name(opp_id, teams.get(opp_id, {}).get('name', str(opp_id)))
                weekly_totals.setdefault(week_num, 0)
                weekly_totals[week_num] += int(pts or 0)
                # keep last entry per week (there should be one match per week)
                weekly_details[week_num] = {
                    'week': week_num,
                    'date': date_str,
                    'opponent': opp_name,
                    'us': int(pts or 0),
                    'them': int(opp_pts or 0),
                    'outcome': 'W' if int(pts or 0) > int(opp_pts or 0) else ('L' if int(pts or 0) < int(opp_pts or 0) else 'T')
                }
            weeks_sorted = sorted(weekly_totals.keys())
            return weeks_sorted, weekly_totals, weekly_details

        weeks8, totals8, details8 = build_weekly_for(trend_team_8)
        weeks9, totals9, details9 = build_weekly_for(trend_team_9)

        # Build weekly_all for client-side filtering
        for tid in teams.keys():
            w8, t8, d8 = build_weekly_for(tid)
            w9, t9, d9 = build_weekly_for(tid)
            weekly_all[tid] = {
                'eight': [d8[w] for w in sorted(d8.keys())],
                'nine': [d9[w] for w in sorted(d9.keys())],
            }

        if weeks8 or weeks9:
            current_week = max(weeks8 + weeks9) if (weeks8 + weeks9) else 0

        # Build trend data from weekly totals (cumulative), last N played
        last_weeks = sorted(w for w in set(weeks8 + weeks9) if w > 0)
        last_weeks = last_weeks[-weekly_count:]
        lead8 = max([t.get('points', 0) for t in standings_data.get('eight_ball', [])], default=0)
        lead9 = max([t.get('points', 0) for t in standings_data.get('nine_ball', [])], default=0)
        for w in last_weeks:
            trend_labels.append(f"W{w}")
            cum8 = sum(totals8.get(x, 0) for x in weeks8 if x <= w)
            cum9 = sum(totals9.get(x, 0) for x in weeks9 if x <= w)
            trend_eight.append(cum8)
            trend_nine.append(cum9)
            trend_target8.append(lead8)
            trend_target9.append(lead9)

        weekly_scores_8 = [details8[w] for w in last_weeks if w in details8]
        weekly_scores_9 = [details9[w] for w in last_weeks if w in details9]
    except Exception as e:
        logging.error(f"Error building trend data: {e}")
    def _points_behind(label_key: str):
        teams_block = standings_data.get(label_key, [])
        target_points = 0
        diff = 0
        for idx, team in enumerate(teams_block):
            if normalize_team_name(team.get('team', '')) == target_team_key:
                target_points = int(team.get('points', 0) or 0)
                if idx > 0:
                    prev = teams_block[idx - 1]
                    diff = max(int(prev.get('points', 0) or 0) - target_points, 0)
                break
        return diff

    def _weekly_delta(entries):
        if not entries or len(entries) < 2:
            return 0
        last = entries[-1].get('us') or 0
        prev = entries[-2].get('us') or 0
        return last - prev

    patch_index = load_patch_index()
    patch_total = sum(len(cat.get('patches', [])) for cat in (patch_index.get('categories') or []))
    points_behind_8 = _points_behind('eight_ball')
    points_behind_9 = _points_behind('nine_ball')
    win_trend_8 = _weekly_delta(weekly_scores_8)
    win_trend_9 = _weekly_delta(weekly_scores_9)
    weekly_improvement_score = max(0, win_trend_8) + max(0, win_trend_9)

    kpi_points_behind = f"8-Ball: {points_behind_8} pts • 9-Ball: {points_behind_9} pts"
    def _trend_label(value):
        if value > 0:
            return f"+{value} pts"
        if value < 0:
            return f"{value} pts"
        return "Stable"
    kpi_win_trend = f"8-Ball: {_trend_label(win_trend_8)} / 9-Ball: {_trend_label(win_trend_9)}"
    performance_summary = {
        "points_behind": kpi_points_behind,
        "win_trend": kpi_win_trend,
        "patches": patch_total,
        "weekly_improvement": weekly_improvement_score
    }

    trend_data = {
        "labels": trend_labels,
        "eight": trend_eight,
        "nine": trend_nine,
        "target8": trend_target8,
        "target9": trend_target9,
        "weekly8": weekly_scores_8,
        "weekly9": weekly_scores_9,
        "weekly_count": weekly_count,
        "team_base": trend_team_base,
        "team_pairs": team_pairs,
        "weekly_all": weekly_all,
    }
    # Debug: Print all data passed to the template
    logging.info("DASHBOARD DATA DUMP:")
    logging.info(f"standings: {standings}")
    logging.info(f"total_owed: {total_owed}")
    logging.info(f"chart_data: {chart_data}")
    logging.info(f"trend_data: {trend_data}")
    logging.info(f"schedule_next: {schedule_next}")
    logging.info(f"recent_results: {recent_results}")
    logging.info(f"dues_table: {dues_table}")
    logging.info(f"dues: {dues}")
    return render_template(
        'dashboard.html',
        standings=standings,
        total_owed=total_owed,
        chart_data=chart_data,
        trend_data=trend_data,
        schedule_next=schedule_next,
        recent_results=recent_results,
        leaderboard_8=leaderboard_8,
        leaderboard_9=leaderboard_9,
        dues_table=dues_table,
        dues=dues,
        standings_8_chart=standings_8_chart,
        standings_9_chart=standings_9_chart,
        points_combo=points_combo,
        days_until_next_game=days_until_next_game,
        current_week=current_week,
        performance_summary=performance_summary,
        target_team_name=target_team_name
    )



# --- Robust Roster Route (user drop-in) ---
@app.route('/roster')
def roster():
    """Render roster for the user's linked team (fallback to Cue-ligans)."""
    data = load_league_data()
    teams = data.get('teams', {}) or {}
    divisions = data.get('divisions', {}) or {}
    try:
        all_teams = build_rosters_from_league(data)
    except Exception as e:
        logging.error(f"Error loading league data for roster: {e}")
        all_teams = []
    team_name = 'Cue-ligans'
    division_label = 'Thursday Brownsville DJ'
    target_key = MY_TEAM_KEY
    user = get_current_user()
    selected_team_record = None
    if user and user.team_id:
        tid = str(user.team_id)
        selected_team_record = teams.get(tid) or teams.get(int(tid) if tid.isdigit() else tid)
        if selected_team_record:
            raw_name = selected_team_record.get('name') or selected_team_record.get('team_name') or str(tid)
            target_key = normalize_team_name(raw_name)
            team_name = pretty_team_name(tid, raw_name)
            div_id = selected_team_record.get('division_id')
            if div_id:
                div = divisions.get(str(div_id)) or divisions.get(div_id, {})
                division_label = get_division_label(str(div_id), div)
    selected_entry = None
    if target_key:
        selected_entry = next((t for t in all_teams if normalize_team_name(t.get('team', '')) == target_key), None)
    if not selected_entry:
        selected_entry = next((t for t in all_teams if normalize_team_name(t.get('team', '')) == MY_TEAM_KEY), None)
    if selected_entry:
        team_name = selected_entry.get('team', team_name)
        if not division_label or division_label == 'Thursday Brownsville DJ':
            # try to infer from data if we know division id
            if selected_team_record and selected_team_record.get('division_id'):
                div = divisions.get(str(selected_team_record.get('division_id'))) or divisions.get(selected_team_record.get('division_id'), {})
                if div:
                    division_label = get_division_label(str(selected_team_record.get('division_id')), div)
        roster_8 = selected_entry.get('eight_ball', [])
        roster_9 = selected_entry.get('nine_ball', [])
    else:
        division_label = 'Thursday Brownsville DJ'
        roster_8 = []
        roster_9 = []

    def prep(roster):
        prepped = []
        for p in roster:
            win_pct = p.get('win_pct', 0)
            try:
                win_pct = float(str(win_pct).replace('%', ''))
            except Exception:
                win_pct = 0.0
            matches = p.get('matches', '0/0')
            try:
                won = int(str(matches).split('/')[0])
            except Exception:
                won = 0
            prepped.append({
                'name': p.get('name', ''),
                'sl': p.get('skill_level') or 0,
                'matches': matches,
                'won': won,
                'win_pct': round(win_pct, 2),
                'ppm': p.get('ppm', 0),
                'pa': float(str(p.get('pa', '0')).replace('%', '')) if p.get('pa') is not None else 0,
            })
        return prepped

    roster_8 = prep(roster_8)
    roster_9 = prep(roster_9)

    unique_ids = set()
    for p in roster_8 + roster_9:
        unique_ids.add(p.get('name', '').strip().lower())
    total_players = len(unique_ids)

    def avg_skill(roster):
        return round(sum(int(p.get('sl') or 0) for p in roster) / len(roster), 1) if roster else 0

    def avg_win(roster):
        return round(sum(float(p.get('win_pct', 0)) for p in roster) / len(roster), 2) if roster else 0

    return render_template(
        'roster.html',
        team_name=team_name,
        division=division_label,
        roster_8=roster_8,
        roster_9=roster_9,
        total_players=total_players,
        avg_sl_8=avg_skill(roster_8),
        avg_sl_9=avg_skill(roster_9),
        avg_win_8=avg_win(roster_8),
        avg_win_9=avg_win(roster_9),
    )

# --- Error handlers ---

# Error Handlers (move to just above main entry point)
@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template('500.html'), 500


# --- Robust Standings Route (user drop-in) ---
@app.route('/standings')
def standings():
    try:
        standings_data = get_standings_from_league()
        if not isinstance(standings_data, dict):
            standings_data = {}
    except Exception as e:
        logging.error(f"Error loading league data for standings page: {e}")
        standings_data = {}
    sorted_8 = sorted(standings_data.get('eight_ball', []), key=lambda x: x.get('rank', 0))
    sorted_9 = sorted(standings_data.get('nine_ball', []), key=lambda x: x.get('rank', 0))
    cue_ligans_idx_8 = next((idx for idx, team in enumerate(sorted_8) if normalize_team_name(team.get('team', '')) == MY_TEAM_KEY), -1)
    cue_ligans_idx_9 = next((idx for idx, team in enumerate(sorted_9) if normalize_team_name(team.get('team', '')) == MY_TEAM_KEY), -1)
    top5_8 = sorted_8[:5]
    top5_9 = sorted_9[:5]
    return render_template(
        'standings.html',
        standings_8=sorted_8,
        standings_9=sorted_9,
        cue_ligans_idx_8=cue_ligans_idx_8,
        cue_ligans_idx_9=cue_ligans_idx_9,
        top5_8=top5_8,
        top5_9=top5_9
    )


@app.route('/mvp')
def mvp():
    league = load_league_data()
    session_name = league.get('meta', {}).get('session') or 'Current Session'
    fmt_param = canonical_format(request.args.get('format'))
    division_param = request.args.get('division')
    division_id, division = select_division_for_format(league, fmt_param, division_param)
    division_name = division.get('name') or division.get('division_id') or 'Division'
    players = []
    if division_id:
        players = gather_mvp_players(league, division_id, fmt_param, session_name)
    min_matches = safe_int(request.args.get('min_matches') or 5)
    raw_team_filter = request.args.get('team')
    team_filter = str(raw_team_filter) if raw_team_filter else 'all'
    qualification_flag = request.args.get('qualified', '')
    show_only_qualified = str(qualification_flag).lower() in ('1', 'true', 'yes')
    team_options = []
    if division:
        teams = league.get('teams', {}) or {}
        team_ids = division.get('team_ids') or [tid for tid, t in teams.items() if t.get('division_id') == division_id]
        for tid in team_ids:
            team = teams.get(tid) or {}
            label = pretty_team_name(tid, team.get('name') or tid)
            team_options.append({'id': str(tid), 'label': label})
    selected_team_label = 'All Teams'
    if team_filter != 'all' and team_options:
        label_match = next((t for t in team_options if t['id'] == team_filter), None)
        if label_match:
            selected_team_label = label_match['label']
    def qualifies(player):
        return player['matches_played'] >= min_matches

    for player in players:
        player['is_qualified'] = qualifies(player)
    filtered_players = [
        p for p in players
        if (team_filter == 'all' or str(p.get('team_id')) == team_filter)
           and (not show_only_qualified or p['is_qualified'])
    ]
    sorted_players = sorted(
        filtered_players,
        key=lambda p: (-p['win_pct'], -p['ppm'], -p['matches_played'])
    )
    for idx, player in enumerate(sorted_players, start=1):
        player['mvp_rank'] = idx
        player['display_win_pct'] = f"{player['win_pct']*100:.1f}%"
        player['display_ppm'] = f"{player['ppm']:.2f}"
        player['display_pa'] = f"{player['pa']*100:.1f}%"
        player['display_points'] = f"{player['total_points']:.2f}"

    available_formats = ['8-ball', '9-ball']
    return render_template(
        'mvp.html',
        session_name=session_name,
        division_name=division_name,
        division_id=division_id,
        available_formats=available_formats,
        current_format=fmt_param,
        mvp_players=sorted_players,
        min_matches=min_matches,
        team_filter=team_filter,
        show_only_qualified=show_only_qualified,
        team_options=team_options,
        qualification_threshold=min_matches,
        selected_team_label=selected_team_label
    )

# --- Main entry point ---
# (Keep at the bottom so the app object is defined before serving)
if __name__ == '__main__':
    app.run(debug=True)
