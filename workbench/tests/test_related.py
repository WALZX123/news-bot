from datetime import datetime, timedelta, timezone
from unittest import TestCase

from workbench.backend.related import related_stories


STAMP = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)


def story(article_id, title, *, hours=0, url=None):
    return {"id": article_id, "title": title, "url": url or f"https://example.org/story/{article_id}",
            "published_at": STAMP + timedelta(hours=hours), "created_at": STAMP}


class RelatedStoriesTest(TestCase):
    def test_labeled_pairs(self):
        pairs = [
            ("OpenAI 发布 GPT‑6.2，两种变体同时上线", "Introducing GPT-6.2 Astra and Sol", 2, "model_action"),
            ("英伟达发布RTX 5090旗舰显卡", "NVIDIA launches GeForce RTX 5090", -3, "model_action"),
            ("特斯拉发布 Model Y L 长轴距版", "Tesla launches Model Y L", 1, "model_action"),
            ("AMD 推出 Instinct MI350X 加速卡", "AMD unveils MI350X for data centers", 1, "model_action"),
            ("OpenAI 发布 GPT-6.2；另一家厂商更新电池；行业数据速览", "Introducing GPT-6.2 Astra and Sol", 1, "roundup"),
            ("OpenAI 发布 GPT-6.2；另一家厂商更新电池；行业数据速览", "GPT-6.2 刚发布，我做了上手实测", 1, "launch_review"),
            ("宁德时代公布面向商用车的换电网络扩张计划", "宁德时代公布面向商用车的换电网络扩张计划！", 3, "similar_title"),
            ("OpenAI 发布 GPT-6.2", "OpenAI updates GPT-6.2 usage limits", 1, None),
            ("英伟达发布 RTX 5090 旗舰显卡", "英伟达发布 RTX 5080 旗舰显卡", 1, None),
            ("特斯拉推出 Model Y L 长轴距版", "Tesla launches Model Y", 1, None),
            ("谷歌发布 Gemini 4", "Google will launch Gemini 4", 1, None),
            ("AMD 发布 2026 年第三季度财报，公布最新业绩", "AMD 发布 2026 年第二季度财报，公布最新业绩", 2, None),
            ("AI 行业新闻日报：模型与芯片动态", "AI 行业新闻日报：模型与芯片动态", 24, None),
            ("OpenAI 发布 GPT-6.2", "Introducing GPT-6.2 Astra and Sol", 49, None),
        ]
        for left, right, hours, expected in pairs:
            with self.subTest(left=left, right=right):
                anchor, other = story(1, left), story(2, right, hours=hours)
                matches = related_stories(anchor, [other])
                self.assertEqual([(other, expected)] if expected else [], matches)

    def test_tracking_urls_match_and_document_parameters_remain_significant(self):
        anchor = story(1, "关于一件事", url="https://example.org/article?id=1&utm_medium=email")
        same = story(2, "标题完全改写", url="https://example.org/article?id=1&gclid=tracking")
        other = story(3, "另一件事", url="https://example.org/article?id=2&utm_source=test")
        duplicate = story(4, "另一个标题", hours=1, url="https://example.org/article?id=1&utm_source=feed")
        self.assertEqual([(same, "same_link")], related_stories(anchor, [other, duplicate, same]))

    def test_dates_fall_back_to_ingest_and_results_are_bounded(self):
        anchor = story(1, "OpenAI 发布 GPT-6.2")
        candidates = [story(number, "Introducing GPT-6.2 Astra and Sol", hours=number)
                      for number in range(2, 13)]
        candidates[0]["published_at"] = None
        self.assertEqual(list(range(2, 10)), [row["id"] for row, _ in related_stories(anchor, candidates)])
