# Railway 배포 멈춤 방지용 더미 파일
# 실제 DB 초기화는 app.py의 lifespan에서 실행됩니다.

if __name__ == "__main__":
    print("✅ init_db.py: Skipping execution to allow Uvicorn to start.")
    # 아무 작업도 하지 않고 정상 종료