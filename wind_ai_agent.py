import asyncio
import json
import os
from datetime import datetime
from typing import List, Optional
from pathlib import Path
from crawl4ai import BrowserConfig, CrawlerRunConfig
from urllib.parse import urlparse, urlunparse
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
import re
# 核心组件
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig, CacheMode, LLMConfig
from crawl4ai.extraction_strategy import LLMExtractionStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

# 导入你的 Schema
from wind_schema import WindExtraction, WindArticle


class WindDeepAgent:
    # def __init__(self, ollama_model: str = "ollama/qwen3-coder:480b-cloud"):
    #     self.output_dir = Path("wind_intelligence_data")
    #     self.output_dir.mkdir(parents=True, exist_ok=True)
    #
    #     # 配置 本地ollama LLM
    #     self.llm_config = LLMConfig(provider=ollama_model)
    #
    #     # 并发控制：同时处理 1 个详情页，避免 Ollama/API 过载
    #     self.semaphore = asyncio.Semaphore(1)
    def __init__(self):
        self.output_dir = Path("wind_intelligence_data")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 1. 设置 API 地址 (DeepSeek 官方地址)
        os.environ["OPENAI_API_BASE"] = "https://api.deepseek.com"

        # 2. 设置 API Key (必须替换为你自己的 sk-xxxx)
        os.environ["OPENAI_API_KEY"] = ""

        # 3. 初始化配置
        self.llm_config = LLMConfig(
            provider="openai/deepseek-chat",
        )

        # 4. 这里的并发可以开大，云端处理很快
        self.semaphore = asyncio.Semaphore(5)

    async def run(self, start_url: str):
        """主流程：列表抓取 -> 详情抓取 -> 报告生成"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_folder = self.output_dir / f"task_{timestamp}"
        run_folder.mkdir(exist_ok=True)

        # --- 步骤 1: 抓取列表页 ---
        print(f"🔥 [Stage 1] 正在扫描列表页: {start_url}")
        extraction_data = await self._crawl_listing_page(start_url)

        if not extraction_data or not extraction_data.news_articles:
            print("❌ 未发现文章链接，任务终止。")
            return

        print(f"✅ 发现 {len(extraction_data.news_articles)} 篇文章，准备进行深度分析...")


        # --- 步骤 2: 并发抓取详情页 ---

        target_articles = extraction_data.news_articles[:5]#把 [:5] 去掉就可以跑全量

        tasks = []
        for article in target_articles:
            tasks.append(self._process_detail_page(article, run_folder))

        # 等待所有详情页任务完成
        detailed_articles = await asyncio.gather(*tasks)

        # 过滤掉抓取失败的 (None)
        valid_articles = [art for art in detailed_articles if art is not None]

        # 更新主数据对象
        extraction_data.news_articles = valid_articles

        # --- 步骤 3: 保存与报告 ---
        self._save_final_data(extraction_data, run_folder)
        self._generate_markdown_report(extraction_data, run_folder)

    async def _crawl_listing_page(self, url: str) -> Optional[WindExtraction]:
        """阶段一：通用启发式提取引擎 (无特定网站依赖)"""
        from urllib.parse import urljoin, urlparse
        import re

        print(f"⚡ [Stage 1] 正在通过通用引擎分析列表页: {url}")

        # 1. 基础配置 (仅使用最通用的参数)
        browser_conf = BrowserConfig(headless=True)
        config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=60000,
            wait_until="domcontentloaded",
        )

        async with AsyncWebCrawler(config=browser_conf) as crawler:
            try:
                result = await crawler.arun(url=url, config=config)
                if not result.success: return None
            except Exception as e:
                print(f"❌ 连接异常: {e}");
                return None

            unique_articles = {}
            # 动态获取当前站点的主域名，不再写死
            parsed_start_url = urlparse(url)
            current_host = parsed_start_url.netloc
            # 获取根域名 (例如: fd.bjx.com.cn -> bjx.com.cn)
            domain_parts = current_host.split('.')
            root_domain = ".".join(domain_parts[-2:]) if len(domain_parts) >= 2 else current_host

            # 2. 增强提取逻辑 (通用特征)
            # A. 排除关键词：不管是哪个站，这些通常都不是新闻
            exclude_pattern = re.compile(
                r'(about|contact|join|login|register|copyright|help|search|feedback|service|career|privacy|member|legal|apply|关于|联系|招聘|声明|登录|注册|下载)',
                re.I)

            # B. 遍历所有链接 (Crawl4AI 已经解析好了基础数据)
            all_links = result.links.get("internal", []) + result.links.get("external", [])
            print(f"🔎 发现原始链接: {len(all_links)} 个")

            for link in all_links:
                href = link.get('href', '')
                title = link.get('text', '').strip()
                full_url = urljoin(url, href)
                parsed_link = urlparse(full_url)

                # --- 过滤器 1: 域名安全检查 ---
                if root_domain not in parsed_link.netloc:
                    continue

                # --- 过滤器 2: 排除非新闻页面 ---
                if exclude_pattern.search(full_url) or exclude_pattern.search(title):
                    continue

                # --- 过滤器 3: 详情页特征评分 (核心通用逻辑) ---
                path = parsed_link.path.lower()

                # 特征 A: URL 包含 4 位及以上连续数字 (这是新闻 ID 或日期的通用标志)
                has_id_feature = len(re.findall(r'\d{4,}', path)) > 0

                # 特征 B: 常见新闻文章后缀
                is_article_ext = path.endswith(('.html', '.shtml', '.htm', '.php', '.jsp')) or path == ""

                # 特征 C: 路径深度。详情页通常在 /news/2024/01.html (深度 >= 2)
                # 列表页通常是 /news/ (深度 = 1)
                path_depth = len([p for p in path.split('/') if p])

                # --- 标题补全逻辑 ---
                # 如果文字为空，尝试从 result.html 中暴力提取该链接对应的 title 属性
                # 这在很多老旧或图片较多的网站中非常管用
                if len(title) < 5:
                    # 尝试正则从源码中找该 href 对应的 title 属性
                    # <a ... href="xxx" ... title="这是标题" ...>
                    attr_match = re.search(fr'href=["\']{re.escape(href)}["\'][^>]*title=["\']([^"\']+)["\']',
                                           result.html, re.I)
                    if attr_match:
                        title = attr_match.group(1).strip()

                # --- 综合判定 ---
                # 规则：(有数字 ID 且 是静态页) 或者 (路径足够深 且 标题长)
                if (has_id_feature and is_article_ext) or (path_depth >= 2 and len(title) > 12):
                    if full_url not in unique_articles:
                        unique_articles[full_url] = WindArticle(
                            title=title if title else "未捕获标题",
                            url=full_url,
                            category="其他"
                        )

            # 3. 结果兜底：如果一条都没抓到，采用“链接模式统计法”
            # 有些网站就是没后缀没 ID，此时我们抓取所有包含本站域名且标题够长的链接
            if not unique_articles:
                print("⚠️ 启发式评分未命中，尝试基于标题长度的通用扫描...")
                for link in all_links:
                    full_url = urljoin(url, link.get('href', ''))
                    title = link.get('text', '').strip()
                    if root_domain in full_url and len(title) > 15:
                        if full_url not in unique_articles:
                            unique_articles[full_url] = WindArticle(title=title, url=full_url, category="其他")

            print(f"📊 通用识别完成: 找到 {len(unique_articles)} 条潜在新闻链接")

            # 将字典转回列表
            data_obj = WindExtraction(
                source_url=url,
                website_title="行业深度情报分析",
                news_articles=list(unique_articles.values()),
                wind_keywords=["WindPower", "Energy_Intelligence"]
            )
            return data_obj

    async def _process_detail_page(self, simple_article: WindArticle, folder: Path) -> Optional[WindArticle]:
        """阶段二：详情页提取"""

        # 必须导入正则模块
        import re

        async with self.semaphore:
            print(f"   ⬇️ 进入详情: {simple_article.title[:15]}...")

            # 1. 浏览器伪装 (User-Agent)
            browser_conf = BrowserConfig(
                headless=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                }
            )

            # 2. 提示词：强调日期和摘要
            strategy = LLMExtractionStrategy(
                llm_config=self.llm_config,
                schema=WindArticle.model_json_schema(),
                instruction="""
                你是一位资深的风电行业情报分析师。请从网页内容提取关键信息。
                
                ### 1. 核心任务：文章分类 (category)
                请根据文章的核心主旨，将 `category` 字段严格设定为以下三者之一：
                - **"技术"**: 涉及风机制造、叶片材料、运维技术、施工工艺、新产品发布、技术参数、专利、科研成果等。
                - **"政策"**: 涉及国家/地方发布的通知、十四五规划、电价补贴、管理办法、行业标准、核准批复、竞配规则等。
                - **"市场"**: 涉及中标结果、企业动态、投融资、装机数据统计等（如果不属于技术或政策，归为此类）。

                ### 2. 其他提取任务
                - **日期 (publish_date)**: 寻找发布时间，格式必须为 YYYY-MM-DD。
                - **摘要 (summary)**: 总结核心事实（涉及的金额、具体参数、相关公司），150字以内，客观陈述。
                - **标签 (tags)**: 提取 3-5 个具体的实体关键词。
                """
            )

            # 3. 运行配置
            config = CrawlerRunConfig(
                extraction_strategy=strategy,
                cache_mode=CacheMode.BYPASS,

                # 这里绝对不能放 CSS 类名(如 .ads)，只能放标签名！
                excluded_tags=["nav", "footer", "script", "style", "noscript", "aside"],

                # 自动移除遮罩层
                remove_overlay_elements=True,

                # 等待页面加载
                delay_before_return_html=3.0,
            )

            async with AsyncWebCrawler(config=browser_conf) as crawler:
                try:
                    result = await crawler.arun(url=simple_article.url, config=config)
                except Exception as e:
                    print(f"   ❌ 浏览器崩溃: {e}")
                    return None

                # 检查是否是 Crawl4AI 的错误页面
                if not result.success or (result.markdown and "Crawl4AI Error" in result.markdown):
                    print(f"   ❌ 抓取被拦截或报错: {simple_article.url}")
                    return None

                if result.extracted_content:
                    try:
                        raw = json.loads(result.extracted_content)
                        if not raw:
                            data_dict = {}
                        else:
                            data_dict = raw[0] if isinstance(raw, list) else raw

                        # 基础数据回填
                        if not data_dict.get('title'): data_dict['title'] = simple_article.title
                        data_dict['url'] = simple_article.url

                        # 摘要兜底：如果 LLM 没生成，手动截取前200字
                        if not data_dict.get('summary'):
                            clean_md = result.markdown[:300].replace('\n', ' ') if result.markdown else ""
                            data_dict['summary'] = f"{clean_md}..."

                        ai_date = data_dict.get('publish_date')

                        # 如果 AI 没填日期，或者日期看起来不对，我们自己搜
                        if not ai_date or len(str(ai_date)) < 8:
                            full_text = result.markdown or result.cleaned_html or ""

                            # 正则匹配：2026-01-09 或 2026年1月9日
                            match = re.search(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', full_text)

                            if match:
                                y, m, d = match.groups()
                                found_date = f"{y}-{int(m):02d}-{int(d):02d}"
                                data_dict['publish_date'] = found_date
                            else:
                                # 最后的尝试：在 URL 里找日期 (有些 URL 包含日期)
                                url_match = re.search(r'202\d{5}', simple_article.url)
                                if url_match:
                                    d_str = url_match.group()
                                    data_dict['publish_date'] = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"

                        # ===============================================

                        rich_article = WindArticle(**data_dict)
                        # 打印日志：标题 + 日期
                        print(f"   ✅ 分析成功: {rich_article.title[:10]}... | 📅 {rich_article.publish_date}")
                        return rich_article

                    except Exception as e:
                        print(f"   ❌ 数据处理异常: {e}")
                        return None
                else:
                    print(f"   ⚠️ 无内容返回")
                    return None

    def _save_final_data(self, data: WindExtraction, folder: Path):
        """保存完整的 JSON 数据"""
        file_path = folder / "full_data.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data.model_dump(), f, ensure_ascii=False, indent=2)
        print(f"💾 结构化数据已保存: {file_path}")

    def _generate_markdown_report(self, data: WindExtraction, folder: Path):
        """生成 Markdown 报告 (按类别分组展示)"""
        report_path = folder / "Wind_Analysis_Report.md"

        md = f"""# 🌬️ {data.website_title} - 深度分析报告

**来源**: {data.source_url}  
**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}  
**全站关键词**: {', '.join(data.wind_keywords)}

---
"""

        grouped_articles = {
            "政策": [],
            "技术": [],
            "市场": [],
            "其他": []
        }

        for art in data.news_articles:
            # 容错处理：如果 LLM 输出的不是标准词，做一个简单的映射
            cat = art.category
            if "政策" in cat or "Policy" in cat:
                grouped_articles["政策"].append(art)
            elif "技术" in cat or "Tech" in cat:
                grouped_articles["技术"].append(art)
            elif "市场" in cat or "Market" in cat:
                grouped_articles["市场"].append(art)
            else:
                grouped_articles["其他"].append(art)

        # === 遍历分组生成报告 ===
        # 定义显示顺序和图标
        categories_order = [
            ("📜 政策法规", grouped_articles["政策"]),
            ("⚙️ 前沿技术", grouped_articles["技术"]),
            ("📈 市场动态", grouped_articles["市场"]),
            ("🔗 其他资讯", grouped_articles["其他"])
        ]

        for title, articles in categories_order:
            if not articles:
                continue

            md += f"\n## {title} (共 {len(articles)} 篇)\n\n"

            for idx, art in enumerate(articles, 1):
                flags = []
                if art.has_project_info: flags.append("🏗️ 项目")
                if art.has_technical_specs: flags.append("📏 参数")
                flag_str = f"| **特征**: {' '.join(flags)}" if flags else ""

                md += f"### {idx}. {art.title}\n"
                md += f"- **日期**: {art.publish_date} | **标签**: `{', '.join(art.tags)}` {flag_str}\n"

                # 引用块摘要
                summary_text = art.summary.replace('\n', '\n> ') if art.summary else "暂无摘要"
                md += f"> {summary_text}\n\n"
                md += f"[🔗 阅读原文]({art.url})\n\n"

            md += "---\n"

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"📝 分类报告已生成: {report_path}")


async def main():
    # 替换目标网站
    target_url = "https://www.woodmac.com/events/global/"
    # https://fd.bjx.com.cn/
    # https://www.in-en.com/
    # https://wind.imarine.cn/offshorewind
    # https://cleantechnica.com/
    # https://www.china5e.com/new-energy/wind-energy/
    # http://www.eastwp.net/news/
    # https://www.woodmac.com/events/global/
    agent = WindDeepAgent()
    await agent.run(target_url)


if __name__ == "__main__":
    asyncio.run(main())