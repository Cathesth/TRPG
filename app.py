import os
import logging
from flask import Flask
from flask_login import LoginManager
from dotenv import load_dotenv

from config import LOG_FORMAT, LOG_DATE_FORMAT, SQLALCHEMY_DATABASE_URI, SQLALCHEMY_TRACK_MODIFICATIONS
from models import db, User, Scenario, Preset, TempScenario, CustomNPC, ScenarioHistory

# 환경 변수 로드
load_dotenv()

# Flask 앱 초기화
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

# DB 설정
app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = SQLALCHEMY_TRACK_MODIFICATIONS

# DB 초기화
db.init_app(app)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT
)

# Flask-Login 설정
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'views.index'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

# 앱 컨텍스트 내에서 테이블 생성 (Railway 배포 시 최초 1회 실행됨)
# 주의: 프로덕션에서는 Flask-Migrate를 사용하는 것이 좋지만, 간편한 배포를 위해 create_all 사용
with app.app_context():
    try:
        db.create_all()
        logging.info("DB Tables created successfully.")
    except Exception as e:
        logging.error(f"DB Creation Failed: {e}")

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

# Blueprint 라우트 등록
from routes import views_bp, api_bp, game_bp

app.register_blueprint(views_bp)
app.register_blueprint(api_bp)
app.register_blueprint(game_bp)

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5001)