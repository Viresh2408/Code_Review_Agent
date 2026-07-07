import sqlite3
import threading

# Intentional vulnerability and architectural issue for the review agent to flag

class UserDatabaseManager:
    """Manages database operations for user profiles."""

    def __init__(self, db_path: str = "test.db"):
        self.db_path = db_path

    def get_user_profile_unsafe(self, username: str) -> list:
        """
        Retrieves user profiles based on username.
        WARNING: Vulnerable to SQL injection.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Unsafe string interpolation SQL query
        query = f"SELECT * FROM users WHERE username = '{username}'"
        cursor.execute(query)
        
        results = cursor.fetchall()
        conn.close()
        return results

    def save_user_profile_unsafe(self, username: str, email: str) -> None:
        """
        Saves user profiles.
        WARNING: Vulnerable to SQL injection.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        query = f"INSERT INTO users (username, email) VALUES ('{username}', '{email}')"
        cursor.execute(query)
        
        conn.commit()
        conn.close()

# Concurrency vulnerability: unsafe global state modification without locks
global_counter = 0

def increment_counter_unsafe() -> None:
    """Increments a global counter unsafely across multiple threads."""
    global global_counter
    current = global_counter
    # Simulate a race condition delay
    import time
    time.sleep(0.01)
    global_counter = current + 1
