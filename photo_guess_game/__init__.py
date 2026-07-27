"""Telegram Spy Game (لعبة الجاسوس والكلمة السرية).

Layering, innermost first:

``models``          plain dataclasses; no I/O, no imports from siblings.
``locations``       the curated secret-word library.
``session_store``   in-memory session storage, per-group locks, generations.
``session_manager`` the rules engine. Every method is synchronous and returns
                    an ``OperationResult`` describing what should be sent; it
                    never performs I/O itself.
``telegram_adapter``the I/O boundary. Runs a decision under the group lock,
                    then performs every network send outside the lock.
``telegram_api``    stdlib-only Bot API client (no third-party dependencies).

The package name is historical: this codebase began as a photo-guessing game.
The photo-specific components (score/guess trackers, photo labels) were removed
once the game became the spy game; see git history if they are ever needed.
"""
