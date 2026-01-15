from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class WindArticle(BaseModel):
    """风电新闻文章"""
    title: str = Field(..., description="文章标题")
    url: str = Field(..., description="文章完整的绝对链接路径")
    summary: Optional[str] = Field(None, description="文章核心摘要，涵盖主要事实，约100-150字")

    category: Literal["技术", "政策", "市场", "其他"] = Field(
        "其他",
        description="文章核心分类，严格从['技术', '政策', '市场', '其他']中选一个"
    )

    publish_date: Optional[str] = Field(
        None,
        description="发布日期，格式必须统一为 YYYY-MM-DD"
    )

    has_project_info: bool = Field(False, description="是否包含项目名称、装机量等信息")
    has_technical_specs: bool = Field(False, description="是否包含风机型号、叶轮直径等技术参数")
    tags: List[str] = Field(default_factory=list, description="提取3-5个关键实体标签")


class WindExtraction(BaseModel):
    """风电网站提取结果"""
    source_url: str = Field(..., description="来源URL")
    website_title: str = Field(..., description="网站标题")
    news_articles: List[WindArticle] = Field(default_factory=list)
    wind_keywords: List[str] = Field(default_factory=list)
    page_language: str = Field("zh", description="语言")