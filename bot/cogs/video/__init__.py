from .cog import Video


async def setup(bot):
    await bot.add_cog(Video(bot))
