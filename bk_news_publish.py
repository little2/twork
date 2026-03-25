import asyncio
from news_db import NewsDatabase

async def publish():
    db = NewsDatabase("postgresql://user:pass@localhost:5432/yourdb")
    await db.init()

    # 插入新闻
    news_id = await db.insert_news(
        title="每日简讯",
        text="📰 今日头条内容来了！",
        file_id=None,
        file_type=None,
        button_str='[ [{"text": "阅读全文", "url": "https://example.com/news"}] ]'
    )

    # 指定要发给哪类 business_type 的用户（如 'news'）
    await db.create_send_tasks(news_id, business_type='news')

    print(f"✅ 新闻 ID={{news_id}} 已发布并建立任务",flush=True)

if __name__ == "__main__":
    asyncio.run(publish())