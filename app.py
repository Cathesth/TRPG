"""
TRPG 게임 엔진 - 메인 애플리케이션
모듈화된 구조로 라우트, 서비스, 코어 로직이 분리되어 있습니다.
"""
import os
import logging
from flask import Flask
from dotenv import load_dotenv

from config import LOG_FORMAT, LOG_DATE_FORMAT

# 환경 변수 로드
load_dotenv()

# Flask 앱 초기화
app = Flask(__name__)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT
)


@app.after_request
def add_header(response):
    """캐시 비활성화 헤더 추가"""
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