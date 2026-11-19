"""Validate legacy rows individually before any model batch is constructed."""
from .storage import utc
from .url_safety import normalize_http_url


def validate_records(items):
    valid, invalid = [], []
    for original in items:
        item=dict(original)
        try:
            item['url']=normalize_http_url(item['url'])
            if not isinstance(item['title'],str) or not item['title'].strip():
                raise ValueError('Empty news title')
            for name in ('summary','source'):
                if item.get(name) is None:
                    item[name]=''
                if not isinstance(item[name],str):
                    raise ValueError('Invalid news text')
            if item.get('published_at') is not None:
                utc(item['published_at'])
            utc(item['created_at'])
        except (KeyError,ValueError,TypeError,AttributeError):
            invalid.append(item['id'])
        else:
            valid.append(item)
    return valid,invalid
