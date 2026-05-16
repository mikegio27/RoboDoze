from urllib.parse import urlencode

import aiohttp

from utils.logging import logger


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    async def search(
        self,
        query: str,
        types: tuple[str, ...] = ('Movie', 'Episode', 'MusicVideo'),
        limit: int = 5,
    ) -> list[dict]:
        qs = urlencode({
            'searchTerm': query,
            'IncludeItemTypes': ','.join(types),
            'Fields': 'MediaSources,Overview',
            'Recursive': 'true',
            'Limit': str(limit),
            'api_key': self.api_key,
        })
        url = f'{self.base_url}/Items?{qs}'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data.get('Items', [])
        except Exception as exc:
            logger.warning(f'JellyfinClient.search failed for {query!r}: {exc!r}')
            return []

    def stream_url(self, item_id: str) -> str:
        return f'{self.base_url}/Videos/{item_id}/stream?api_key={self.api_key}&Static=true'

    def audio_url(self, item_id: str) -> str:
        return f'{self.base_url}/Audio/{item_id}/stream?api_key={self.api_key}&Static=true'
