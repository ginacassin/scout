"""Provider-independent Instagram data. Apify and HikerAPI adapters both return these."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PostItem:
    shortcode: str
    url: str
    owner: str
    posted_at: datetime | None
    caption: str
    image_urls: list[str]
    is_pinned: bool = False
    coauthors: list[str] = field(default_factory=list)
    location: str | None = None
    media_type: str = "image"  # image | carousel | video

    @property
    def ig_id(self) -> str:
        return f"post:{self.shortcode}"


@dataclass
class StoryItem:
    story_id: str
    owner: str
    posted_at: datetime | None
    image_url: str | None
    link_urls: list[str] = field(default_factory=list)
    is_video: bool = False
    reshared_from: str | None = None  # author of a story this one reshares
    shared_post_url: str | None = None  # someone else's post shown in this story
    mentions: list[str] = field(default_factory=list)  # @mentions on the story

    @property
    def ig_id(self) -> str:
        return f"story:{self.story_id}"

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/stories/{self.owner}/{self.story_id}/"


@dataclass
class ProfileSnapshot:
    handle: str
    full_name: str | None
    biography: str
    bio_links: list[str]
    posts: list[PostItem]


class InstagramSource(ABC):
    @abstractmethod
    async def profiles(self, handles: list[str]) -> dict[str, ProfileSnapshot]:
        """Bio, bio links and latest posts for each handle. Missing handles are absent from the dict."""

    @abstractmethod
    async def stories(self, handles: list[str]) -> list[StoryItem]:
        """Currently live stories of the given handles."""

    async def spend_usd(self) -> tuple[float, float] | None:
        """(used this month, monthly limit), if the provider reports it."""
        return None
