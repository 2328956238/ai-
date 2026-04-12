import httpx
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
import os
from dotenv import load_dotenv

load_dotenv()

AI_API_KEY = os.getenv("sk-aaaa")#z这是在超算中心的api
AI_BASE_URL = os.getenv("https://api.scnet.cn/api/llm/v1")
AI_MODEL = os.getenv("DeepSeek-R1-Distill-Qwen-7B")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def call_ai(prompt: str) -> str:
    """调用 AI API，返回文本响应"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 300
        }
        response = await client.post(AI_BASE_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

async def enrich_product_batch(titles: list[str], concurrency: int = 5) -> list[dict]:
    """
    并发调用 AI 优化多个商品标题
    返回: [{"title": "原标题", "optimized_description": "...", "category": "..."}, ...]
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def process_one(title: str):
        async with semaphore:
            prompt = f"""请为以下商品标题生成一段吸引人的中文描述（不超过80字）并给出商品类别（从以下选择：电子产品、家居用品、服装配饰、食品饮料、美妆个护、玩具文具、其他）。格式严格为两行：
优化描述：...
类别：... 
商品标题：{title}"""
            try:
                result = await call_ai(prompt)
                lines = result.split("\n")
                description = ""
                category = "其他"
                for line in lines:
                    if line.startswith("优化描述："):
                        description = line.replace("优化描述：", "").strip()
                    elif line.startswith("类别："):
                        category = line.replace("类别：", "").strip()
                return {
                    "original_title": title,
                    "optimized_description": description or f"优质{title}，值得购买",
                    "category": category if category in ["电子产品","家居用品","服装配饰","食品饮料","美妆个护","玩具文具","其他"] else "其他"
                }
            except Exception as e:
                # 失败时返回默认值
                return {
                    "original_title": title,
                    "optimized_description": f"精选{title}，品质保证",
                    "category": "其他",
                    "error": str(e)
                }

    tasks = [process_one(title) for title in titles]
    results = await asyncio.gather(*tasks)
    return results