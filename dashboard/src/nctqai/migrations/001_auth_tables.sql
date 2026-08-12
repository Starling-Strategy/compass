CREATE SCHEMA IF NOT EXISTS nctqai;

-- Users (invite-only)
CREATE TABLE nctqai.users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'analyst', 'power_user', 'admin')),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    created_by INTEGER REFERENCES nctqai.users(id),
    last_login TIMESTAMPTZ
);

-- OTP codes (short-lived)
CREATE TABLE nctqai.auth_codes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES nctqai.users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER DEFAULT 0,
    used BOOLEAN DEFAULT false
);

-- Sessions
CREATE TABLE nctqai.sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES nctqai.users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    ip_address TEXT
);

-- Rate limiting (tracks failed attempts per IP)
CREATE TABLE nctqai.rate_limits (
    id SERIAL PRIMARY KEY,
    ip_address TEXT NOT NULL,
    action TEXT NOT NULL,  -- 'send_code' or 'verify_code'
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX idx_auth_codes_user_id ON nctqai.auth_codes(user_id);
CREATE INDEX idx_sessions_user_id ON nctqai.sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON nctqai.sessions(expires_at);
CREATE INDEX idx_users_email ON nctqai.users(email);
CREATE INDEX idx_rate_limits_ip_action ON nctqai.rate_limits(ip_address, action, created_at);

-- Seed first admin
INSERT INTO nctqai.users (email, name, role)
VALUES ('macon.phillips@gmail.com', 'Macon Phillips', 'admin');
