# © 2018–2020 io mintz <io@mintz.cc>
#
# Emote Manager is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# Emote Manager is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Emote Manager. If not, see <https://www.gnu.org/licenses/>.

import asyncio
import cgi
import collections
import contextlib
import io
import logging
import operator
import posixpath
import re
import traceback
import urllib.parse
import zipfile
import warnings
import weakref
import time
import os
import typing

import aioec
import aiohttp
import discord
import humanize
from discord.ext import commands

import utils
import utils.archive
import utils.image
from utils import errors
from utils.converter import emote_type_filter_default
from utils.paginator import ListPaginator

logger = logging.getLogger(__name__)

# guilds can have duplicate emotes, so let us create zips to match
warnings.filterwarnings('ignore', module='zipfile', category=UserWarning, message=r"^Duplicate name: .*$")

class UserCancelledError(commands.UserInputError):
	pass

class Emotes(commands.Cog):
	IMAGE_MIMETYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
	# TAR_MIMETYPES = {'application/x-tar', 'application/x-xz', 'application/gzip', 'application/x-bzip2'}
	TAR_MIMETYPES = {'application/x-tar'}
	ZIP_MIMETYPES = {'application/zip', 'application/octet-stream', 'application/x-zip-compressed', 'multipart/x-zip'}
	ARCHIVE_MIMETYPES = TAR_MIMETYPES | ZIP_MIMETYPES
	ZIP_OVERHEAD_BYTES = 30

	def __init__(self, bot):
		self.bot = bot

		connector = None
		socks5_url = self.bot.config.get('socks5_proxy_url')
		if socks5_url:
			from aiohttp_socks import SocksConnector
			connector = SocksConnector.from_url(socks5_url, rdns=True)

		self.http = aiohttp.ClientSession(
			loop=self.bot.loop,
			read_timeout=self.bot.config.get('http_read_timeout', 60),
			connector=connector if self.bot.config.get('use_socks5_for_all_connections') else None,
			headers={
				'User-Agent':
					self.bot.config['user_agent'] + ' '
					+ self.bot.http.user_agent
			})

		self.aioec = aioec.Client(
			loop=self.bot.loop,
			connector=connector,
			base_url=self.bot.config.get('ec_api_base_url'))
		# keep track of paginators so we can end them when the cog is unloaded
		self.paginators = weakref.WeakSet()

	def cog_unload(self):
		async def close():
			await self.http.close()
			await self.aioec.close()

			for paginator in self.paginators:
				await paginator.stop()

		self.bot.loop.create_task(close())

	public_commands = set()
	def public(command, public_commands=public_commands):  # resolve some kinda scope issue that i don't understand
		public_commands.add(command.qualified_name)
		return command

	dm_commands = set()
	def dm_ok(command, dm_commands=dm_commands):
		dm_commands.add(command.qualified_name)
		return command

	async def cog_check(self, context):
		if (context.command.qualified_name in self.dm_commands) and (not context.guild):
			return True

		if not context.guild:
			raise commands.NoPrivateMessage

		# we can't just do `context.command in self.public_commands` here
		# because apparently Command.__eq__ is not defined
		if context.command.qualified_name in self.public_commands:
			return True

		if (
			not context.author.guild_permissions.manage_emojis
			or not context.guild.me.guild_permissions.manage_emojis
		):
			raise errors.MissingManageEmojisPermission

		return True

	@commands.Cog.listener()
	async def on_command_error(self, context, error):
		if isinstance(error, errors.EmoteManagerError):
			await context.send(error)

		if isinstance(error, commands.NoPrivateMessage):
			await context.send(
				f'{utils.SUCCESS_EMOJIS[False]} Sorry, this command may only be used in a server.')

		if isinstance(error, commands.CommandOnCooldown):
			await context.send(f'{utils.SUCCESS_EMOJIS[False]} This command has a cooldown. Try again in {int(error.retry_after) + 1} seconds.')

	@commands.command(usage='[name] <image URL or custom emote>')
	async def add(self, context, *args):
		"""Add a new emote to this server.

		You can use it like this:
		`add :thonkang:` (if you already have that emote)
		`add rollsafe https://image.noelshack.com/fichiers/2017/06/1486495269-rollsafe.png`
		`add speedtest <https://cdn.discordapp.com/emojis/379127000398430219.png>`

		With a file attachment:
		`add name` will upload a new emote using the first attachment as the image and call it `name`
		`add` will upload a new emote using the first attachment as the image,
		and its filename as the name
		"""
		name, url = self.parse_add_command_args(context, args)
		async with context.typing():
			message = await self.add_safe(context, name, url, context.message.author.id)
		await context.send(message)

	@commands.command(name='add-these')
	async def add_these(self, context, *emotes):
		"""Add a bunch of custom emotes."""

		ran = False
		# we could use *emotes: discord.PartialEmoji here but that would require spaces between each emote.
		# and would fail if any arguments were not valid emotes
		for match in re.finditer(utils.emote.RE_CUSTOM_EMOTE, ''.join(emotes)):
			ran = True
			animated, name, id = match.groups()
			image_url = utils.emote.url(id, animated=animated)
			async with context.typing():
				message = await self.add_safe(context, name, image_url, context.author.id)
				await context.send(message)

		if not ran:
			return await context.send('Error: no custom emotes were provided.')

		await context.message.add_reaction(utils.SUCCESS_EMOJIS[True])

	@classmethod
	def parse_add_command_args(cls, context, args):
		if context.message.attachments:
			return cls.parse_add_command_attachment(context, args)

		elif len(args) == 1:
			match = utils.emote.RE_CUSTOM_EMOTE.match(args[0])
			if match is None:
				raise commands.BadArgument(
					'Error: I expected a custom emote as the first argument, '
					'but I got something else. '
					"If you're trying to add an emote using an image URL, "
					'you need to provide a name as the first argument, like this:\n'
					'`{}add NAME_HERE URL_HERE`'.format(context.prefix))
			else:
				animated, name, id = match.groups()
				url = utils.emote.url(id, animated=animated)

			return name, url

		elif len(args) >= 2:
			name = args[0]
			match = utils.emote.RE_CUSTOM_EMOTE.match(args[1])
			if match is None:
				url = utils.strip_angle_brackets(args[1])
			else:
				url = utils.emote.url(match['id'], animated=match['animated'])

			return name, url

		elif not args:
			raise commands.BadArgument('Your message had no emotes and no name!')

	@classmethod
	def parse_add_command_attachment(cls, context, args):
		attachment = context.message.attachments[0]
		name = cls.format_emote_filename(''.join(args) if args else attachment.filename)
		url = attachment.url

		return name, url

	@staticmethod
	def format_emote_filename(filename):
		"""format a filename to an emote name as discord does when you upload an emote image"""
		left, sep, right = posixpath.splitext(filename)[0].rpartition('-')
		return (left or right).replace(' ', '')

	@commands.command(name='add-from-ec', aliases=['addfromec'])
	async def add_from_ec(self, context, name, *names):
		"""Copies one or more emotes from Emote Collector to your server.

		The list of possible emotes you can copy is here:
		https://ec.emote.bot/list
		"""
		if names:
			for name in (name,) + names:
				await context.invoke(self.add_from_ec, name)
			return

		name = name.strip(':')
		try:
			emote = await self.aioec.emote(name)
		except aioec.NotFound:
			return await context.send("Emote not found in Emote Collector's database.")
		except aioec.HttpException as exception:
			return await context.send(
				f'Error: the Emote Collector API returned status code {exception.status}')

		reason = (
			f'Added from Emote Collector by {utils.format_user(self.bot, context.author.id)}. '
			f'Original emote author: {utils.format_user(self.bot, emote.author)}')

		async with context.typing():
			message = await self.add_safe(context, name, emote.url, context.author.id, reason=reason)

		await context.send(message)

	@public
	@emote_type_filter_default
	@commands.command()
	@commands.bot_has_permissions(attach_files=True)
	@commands.cooldown(1, 20, type=commands.BucketType.guild)
	async def export(self, context, image_type='all'):
		"""Export all emotes from this server to a zip file, suitable for use with the import command.

		If “animated” is provided, only include animated emotes.
		If “static” is provided, only include static emotes.
		Otherwise, or if “all” is provided, export all emotes.

		This command requires the “attach files” permission.
		"""
		emotes = list(filter(image_type, context.guild.emojis))
		if not emotes:
			raise commands.BadArgument('No emotes of that type were found in this server.')

		async with context.typing():
			async for zip_file in self.archive_emotes(context, emotes):
				await context.send(file=zip_file)

	async def archive_emotes(self, context, emotes):
		filesize_limit = context.guild.filesize_limit
		discrims = collections.defaultdict(int)
		downloaded = collections.deque()
		async def download(emote):
			# don't put two files in the zip with the same name
			discrims[emote.name] += 1
			discrim = discrims[emote.name]
			if discrim == 1:
				name = emote.name
			else:
				name = f'{emote.name}-{discrim}'

			name = f'{name}.{"gif" if emote.animated else "png"}'

			# place some level of trust on discord's CDN to actually give us images
			data = await self.fetch_safe(str(emote.url), validate_headers=False)
			if type(data) is str:  # error case
				await context.send(f'{emote}: {data}')
				return

			est_zip_overhead = len(name) + self.ZIP_OVERHEAD_BYTES
			est_size_in_zip = est_zip_overhead + len(data)
			if est_size_in_zip >= filesize_limit:
				self.bot.loop.create_task(
					context.send(f'{emote} could not be added because it alone would exceed the file size limit.')
				)
				return

			downloaded.append((name, emote.created_at, est_size_in_zip, data))

		await utils.gather_or_cancel(*map(download, emotes))

		count = 1
		while True:
			out = io.BytesIO()
			with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as zip:
				while True:
					try:
						item = downloaded.popleft()
					except IndexError:
						break

					name, created_at, est_size, image_data = item

					if out.tell() + est_size >= filesize_limit:
						# adding this emote would bring us over the file size limit
						downloaded.appendleft(item)
						break

					zinfo = zipfile.ZipInfo(name, date_time=created_at.timetuple()[:6])
					zip.writestr(zinfo, image_data)

				if out.tell() == 0:
					# no emotes were written
					break

			out.seek(0)
			yield discord.File(out, f'emotes-{context.guild.id}-{count}.zip')
			count += 1

	@commands.command(name='import', aliases=['add-zip', 'add-tar', 'add-from-zip', 'add-from-tar'])
	@commands.cooldown(1, 20, type=commands.BucketType.guild)
	async def import_(self, context, url=None):
		"""Add several emotes from a .zip or .tar archive.

		You may either pass a URL to an archive or upload one as an attachment.
		All valid GIF, PNG, and JPEG files in the archive will be uploaded as emotes.
		The rest will be ignored.
		"""
		if url and context.message.attachments:
			raise commands.BadArgument('Either a URL or an attachment must be given, not both.')
		if not url and not context.message.attachments:
			raise commands.BadArgument('A URL or attachment must be given.')

		url = url or context.message.attachments[0].url
		async with context.typing():
			archive = await self.fetch_safe(url, valid_mimetypes=self.ARCHIVE_MIMETYPES)
		if type(archive) is str:  # error case
			await context.send(archive)
			return

		await self.add_from_archive(context, archive)
		with contextlib.suppress(discord.HTTPException):
			# so they know when we're done
			await context.message.add_reaction(utils.SUCCESS_EMOJIS[True])

	async def add_from_archive(self, context, archive):
		limit = 50_000_000  # prevent someone from trying to make a giant compressed file
		async for name, img, error in utils.archive.extract_async(io.BytesIO(archive), size_limit=limit):
			try:
				utils.image.mime_type_for_image(img)
			except errors.InvalidImageError:
				continue
			if error is None:
				name = self.format_emote_filename(posixpath.basename(name))
				async with context.typing():
					message = await self.add_safe_bytes(context, name, context.author.id, img)
				await context.send(message)
				continue

			if isinstance(error, errors.FileTooBigError):
				await context.send(
					f'{name}: file too big. '
					f'The limit is {humanize.naturalsize(error.limit)} '
					f'but this file is {humanize.naturalsize(error.size)}.')
				continue

			await context.send(f'{name}: {error}')

	async def add_safe(self, context, name, url, author_id, *, reason=None):
		"""Try to add an emote. Returns a string that should be sent to the user."""
		try:
			image_data = await self.fetch_safe(url)
		except errors.InvalidFileError:
			raise errors.InvalidImageError

		if type(image_data) is str:  # error case
			return image_data
		return await self.add_safe_bytes(context, name, author_id, image_data, reason=reason)

	async def fetch_safe(self, url, valid_mimetypes=None, *, validate_headers=False):
		"""Try to fetch a URL. On error return a string that should be sent to the user."""
		try:
			return await self.fetch(url, valid_mimetypes=valid_mimetypes, validate_headers=validate_headers)
		except asyncio.TimeoutError:
			return 'Error: retrieving the image took too long.'
		except ValueError:
			return 'Error: Invalid URL.'
		except aiohttp.ClientResponseError as exc:
			raise errors.HTTPException(exc.status)

	async def add_safe_bytes(self, context, name, author_id, image_data: bytes, *, reason=None):
		"""Try to add an emote from bytes. On error, return a string that should be sent to the user.

		If the image is static and there are not enough free static slots, convert the image to a gif instead.
		"""
		counts = collections.Counter(map(operator.attrgetter('animated'), context.guild.emojis))
		# >= rather than == because there are sneaky ways to exceed the limit
		if counts[False] >= context.guild.emoji_limit and counts[True] >= context.guild.emoji_limit:
			# we raise instead of returning a string in order to abort commands that run this function in a loop
			raise commands.UserInputError('This server is out of emote slots.')

		static = utils.image.mime_type_for_image(image_data) != 'image/gif'
		converted = False
		if static and counts[False] >= context.guild.emoji_limit:
			image_data = await utils.image.convert_to_gif_in_subprocess(image_data)
			converted = True

		try:
			emote = await self.create_emote_from_bytes(context.guild, name, author_id, image_data, reason=reason)
		except discord.InvalidArgument:
			return discord.utils.escape_mentions(f'{name}: The file supplied was not a valid GIF, PNG, JPEG, or WEBP file.')
		except discord.HTTPException as ex:
			return discord.utils.escape_mentions(
				f'{name}: An error occurred while creating the the emote:\n'
				+ utils.format_http_exception(ex))
		s = f'Emote {emote} successfully created'
		return s + ' as a GIF.' if converted else s + '.'

	async def fetch(self, url, valid_mimetypes=IMAGE_MIMETYPES, *, validate_headers=True):
		valid_mimetypes = valid_mimetypes or self.IMAGE_MIMETYPES
		def validate_headers(response):
			response.raise_for_status()
			# some dumb servers also send '; charset=UTF-8' which we should ignore
			mimetype, options = cgi.parse_header(response.headers.get('Content-Type', ''))
			if mimetype not in valid_mimetypes:
				raise errors.InvalidFileError

		async def validate(request):
			try:
				async with request as response:
					validate_headers(response)
					return await response.read()
			except aiohttp.ClientResponseError:
				raise
			except aiohttp.ClientError as exc:
				raise errors.EmoteManagerError(f'An error occurred while retrieving the file: {exc}')

		if validate_headers: await validate(self.http.head(url, timeout=self.bot.config.get('http_head_timeout', 10)))
		return await validate(self.http.get(url))

	async def create_emote_from_bytes(self, guild, name, author_id, image_data: bytes, *, reason=None):
		image_data = await utils.image.resize_in_subprocess(image_data)
		if reason is None:
			reason = f'Created by {utils.format_user(self.bot, author_id)}'
		return await guild.create_custom_emoji(name=name, image=image_data, reason=reason)

	@commands.command(aliases=('delete', 'delet', 'rm'))
	async def remove(self, context, emote, *emotes):
		"""Remove an emote from this server.

		emotes: the name of an emote or of one or more emotes you'd like to remove.
		"""
		if not emotes:
			emote = await self.parse_emote(context, emote)
			await emote.delete(reason=f'Removed by {utils.format_user(self.bot, context.author.id)}')
			await context.send(fr'Emote \:{emote.name}: successfully removed.')
		else:
			for emote in (emote,) + emotes:
				await context.invoke(self.remove, emote)
			with contextlib.suppress(discord.HTTPException):
				await context.message.add_reaction(utils.SUCCESS_EMOJIS[True])

	@commands.command(aliases=('mv',))
	async def rename(self, context, old, new_name):
		"""Rename an emote on this server.

		old: the name of the emote to rename, or the emote itself
		new_name: what you'd like to rename it to
		"""
		emote = await self.parse_emote(context, old)
		try:
			await emote.edit(
				name=new_name,
				reason=f'Renamed by {utils.format_user(self.bot, context.author.id)}')
		except discord.HTTPException as ex:
			return await context.send(
				'An error occurred while renaming the emote:\n'
				+ utils.format_http_exception(ex))

		await context.send(fr'Emote successfully renamed to \:{new_name}:')

	@public
	@emote_type_filter_default
	@commands.command(aliases=('ls', 'dir'))
	async def list(self, context, image_type='all'):
		"""A list of all emotes on this server.

		The list shows each emote and its raw form.

		If "animated" is provided, only show animated emotes.
		If "static" is provided, only show static emotes.
		If “all” is provided, show all emotes.
		"""
		emotes = sorted(
			filter(image_type, context.guild.emojis),
			key=lambda e: e.name.lower())

		processed = []
		for emote in emotes:
			raw = str(emote).replace(':', r'\:')
			processed.append(f'{emote} {raw}')

		paginator = ListPaginator(context, processed)
		self.paginators.add(paginator)
		await paginator.begin()

	@public
	@commands.command(aliases=['status'])
	async def stats(self, context):
		"""The current number of animated and static emotes relative to the limits."""
		emote_limit = context.guild.emoji_limit

		static_emotes = animated_emotes = total_emotes = 0
		for emote in context.guild.emojis:
			if emote.animated:
				animated_emotes += 1
			else:
				static_emotes += 1

			total_emotes += 1

		percent_static = round((static_emotes / emote_limit) * 100, 2)
		percent_animated = round((animated_emotes / emote_limit) * 100, 2)

		static_left = emote_limit - static_emotes
		animated_left = emote_limit - animated_emotes

		await context.send(
			f'Static emotes: **{static_emotes} / {emote_limit}** ({static_left} left, {percent_static}% full)\n'
			f'Animated emotes: **{animated_emotes} / {emote_limit}** ({animated_left} left, {percent_animated}% full)\n'
			f'Total: **{total_emotes} / {emote_limit * 2}**')

	@public
	@dm_ok
	@commands.command(aliases=["big", "embiggen"])
	async def show(self, context, emote: typing.Union[discord.PartialEmoji, str]):
		"""Shows the full image for the given emote.
		If the emote is from this server, then it will also show when the emote was added and who added it.

		emote: the emote to show. It can be the emote itself (can be from any server), or the name of the emote (only for emotes from this server).
		"""
		if type(emote) is discord.PartialEmoji:
			await self.show_emote(context, emote)
		elif context.guild:
			ParsedEmote = await self.parse_emote(context, emote)
			await self.show_emote(context, ParsedEmote)
		else:
			await context.send("You must specify an emote.")

	async def show_emote(self, context, emote):
		try:
			emoteFull = await context.guild.fetch_emoji(emote.id)
		except:
			await context.send(f':{emote.name}: {emote.url}')
		else:
			# Sending the message as an embed
			embed = discord.Embed.from_dict({
				"title": "Emote",
				"type": "rich",
				"color": 16763981,
				"image": {
					"url": f"{emoteFull.url}"
				},
				"fields": [{
					"name": "Name",
					"value": f":{emoteFull.name}:"
				}, {
					"name": "URL",
					"value": f"{emoteFull.url}"
				}, {
					"name": "Added by",
					"value": f"{emoteFull.user.name}#{emoteFull.user.discriminator} {emoteFull.user.mention}"
				}],
				"footer": {
					"text": "Emote added on:"
				},
				"timestamp": f"{emoteFull.created_at}"
			})
			await context.send(embed=embed)

	async def parse_emote(self, context, name_or_emote):
		match = utils.emote.RE_CUSTOM_EMOTE.match(name_or_emote)
		if match:
			id = int(match.group('id'))
			emote = discord.utils.get(context.guild.emojis, id=id)
			if emote:
				return emote
		name = name_or_emote
		return await self.disambiguate(context, name)

	async def disambiguate(self, context, name):
		name = name.strip(':')  # in case the user tries :foo: and foo is animated
		candidates = [e for e in context.guild.emojis if e.name.lower() == name.lower() and e.require_colons]
		if not candidates:
			raise errors.EmoteNotFoundError(name)

		if len(candidates) == 1:
			return candidates[0]

		message = ['Multiple emotes were found with that name. Which one do you mean?']
		for i, emote in enumerate(candidates, 1):
			message.append(fr'{i}. {emote} (\:{emote.name}:)')

		await context.send('\n'.join(message))

		def check(message):
			try:
				int(message.content)
			except ValueError:
				return False
			else:
				return message.author == context.author

		try:
			message = await self.bot.wait_for('message', check=check, timeout=30)
		except asyncio.TimeoutError:
			raise commands.UserInputError('Sorry, you took too long. Try again.')

		return candidates[int(message.content)-1]

	@public
	@dm_ok
	@commands.command()
	async def identify(self, context, url, look_for="all"):
		"""Displays names and URLs of all the emotes used in a message or reacted to a message.

		url: The URL of the message you would like to identify the emotes of. Message must be from this server, unless you are running this command in DM.

		If "message" is provided, then only emotes within the linked message will be returned.
		If "reaction" is provided, then only reactions to the linked message will be returned.
		If "all" is provided, then both will be returned. (default)
		"""
		url_match = re.findall("^https://[a-z]*.?discord.com/channels/([0-9]{18})/([0-9]{18})/([0-9]{18})$", url)
		if url_match:
			if (not context.guild) or (url_match[0][0] == str(context.guild.id)):
				try:
					url_channel = self.bot.get_channel(int(url_match[0][1]))
					url_message = await url_channel.fetch_message(int(url_match[0][2]))
				except:
					await context.send(f'{utils.SUCCESS_EMOJIS[False]} I cannot access this message.')
				else:
					results = []
					if (look_for == "message") or (look_for == "all"):
						em_match = re.findall("<(a)?:([A-z0-9-_]{2,32}):([0-9]{18})>", url_message.content)
						if em_match:
							emotes_in = []
							for i in em_match:
								# Removing duplicate list items
								if i not in emotes_in:
									emotes_in.append(i)
							for emote in emotes_in:
								if emote[0]:
									results.append(f":{emote[1]}: https://cdn.discordapp.com/emojis/{emote[2]}.gif")
								else:
									results.append(f":{emote[1]}:\nhttps://cdn.discordapp.com/emojis/{emote[2]}.png")
					if (look_for == "reaction") or (look_for == "all"):
						if url_message.reactions:
							for reaction in url_message.reactions:
								if reaction.custom_emoji:
									results.append(f"(reaction) :{reaction.emoji.name}: {reaction.emoji.url}")
								else:
									results.append(f"(reaction) {reaction.emoji} (Unicode emoji)")
					if results == []:
						await context.send("I did not see any emotes in this message.")
					else:
						paginator = ListPaginator(context, results)
						self.paginators.add(paginator)
						await paginator.begin()
			else:
				await context.send(f'{utils.SUCCESS_EMOJIS[False]} You are only allowed to specify a message from within this server.')
		else:
			await context.send(f"{utils.SUCCESS_EMOJIS[False]} You did not specify a valid URL. You can get a message's URL by right clicking on it and selecting 'Copy Message Link'.")

	@public
	@commands.command(aliases=['html'])
	@commands.bot_has_permissions(attach_files=True)
	@commands.cooldown(1, 20, type=commands.BucketType.guild)
	async def archive(self, ctx):
		"""Outputs an HTML file containing all of the server's emotes.
		At this time, this command does not have any parameters."""

		if not(ctx.guild) or ctx.author.bot:
			return

		print("Recieved archive request")

		emojis = ctx.guild.emojis
		emoteNames = []
		emoteUrls = []
		emoteHtml = []

		for emoji in emojis:
			emoteNames.append(emoji.name)
			emoteUrls.append(emoji.url)

		i = 0
		while i < len(emoteNames):
			emoteHtml.append('\t<img title=":%s:" alt=":%s:" src="%s">' % (emoteNames[i], emoteNames[i], emoteUrls[i]))
			i += 1

		body = "\n".join(emoteHtml)
		serverName = ctx.guild.name
		localtime = time.localtime()
		currentDate = time.strftime("%B %d, %Y", localtime)
		currentTime = time.strftime("%H:%M %Z", localtime)

		fullHtml = '<!DOCTYPE html>\n<html>\n<head>\n\t<!-- Generated by Emote Manager https://github.com/saucylegs/EmoteManager -->\n\t<meta charset="UTF-8">\n\t<meta name="viewport" content="width=device-width, initial-scale=1.0">\n\t<meta name="robots" content="noindex">\n\t<style>\n\t\tbody {\n\t\t\tfont-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu, "Open Sans", sans-serif;\n\t\t\tfont-size: 1.25rem;\n\t\t\tmargin: 8px;\n\t\t\tbackground-color: #2f3136;\n\t\t\tcolor: #ffffff;\n\t\t}\n\t\th1, p {\n\t\t\tmargin: 0.5rem 0;\n\t\t}\n\t\timg {\n\t\t\tmax-width: 64px;\n\t\t\tmax-height: 64px;\n\t\t\tmargin: 5px 2px;\n\t\t\tvertical-align: middle;\n\t\t}\n\t</style>\n\t<title>%s Emote Archive (%s)</title>\n\t<meta name="og:title" content="%s Emote Archive (%s)">\n</head>\n<body>\n\t<h1>%s Emote Archive</h1>\n\t<p>Archived on %s at %s</p>\n\t<p>On some browsers, you can hover over an emote to see its name.</p>\n%s\n</body>\n</html>' % (serverName, currentDate, serverName, currentDate, serverName, currentDate, currentTime, body)

		filepath = "./generated/emote-archive-" + str(time.time_ns()) + ".html"
		f = open(filepath, "x")
		f.write(fullHtml)
		f.close()

		await ctx.send(content=ctx.author.mention, file=discord.File(filepath))

		os.remove(filepath)
		

def setup(bot):
	bot.add_cog(Emotes(bot))
