"""
Slack chat bot for secret santa
"""
from functools import lru_cache

from slackclient import SlackClient

class Bot(object):
    """
    Bot for handling slack interactions
    """
    def __init__(self, client_id, client_secret, signing_secret):
        super().__init__()

        self.name = "secretsantabot"
        self.friendly_name = "Secret Santa Bot"
        self.emoji = ":robot_face:"

        # Save oauth creds
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = "bot"
        # And the verification token
        self.signing_secret = signing_secret

        # Create a blank slack client, that will be filled once we've
        # authenticated
        self.client = SlackClient("")

    @lru_cache()
    def open_dm(self, user):
        """
        Open a direct chat with the given user.
        Note: user should be a valid slack ID
        """
        resp = self.client.api_call("im.open", user=user)
        if not resp["ok"]:
            raise RuntimeError(f"Failed to open dm with {user}")
        return resp["channel"]["id"]

    @lru_cache()
    def get_channel_by_name(self, handle):
        """
        Get the id of the channel with given handle
        """
        resp = self.client.api_call("conversations.list", exclude_archived=True, types="public_channel")
        if not resp["ok"]:
            raise RuntimeError(f"Failed to load channels")
        for channel in resp["channels"]:
            if channel["name"] == handle:
                return channel["id"]
        return None

    def post_message(self, channel, message):
        """
        Post a message to the given channel
        Args:
            channel: channel id to send the message to
            message: the text of the message
        """
        self.client.api_call("chat.postMessage", channel=channel, text=message)

    def auth(self, code):
        """
        Authenticate with OAuth and assign correct scopes.
        Save a dictionary of authed team information in memory on the bot
        object.
        Parameters
        ----------
        code : str
            temporary authorization code sent by Slack to be exchanged for an
            OAuth token
        """
        # After the user has authorized this app for use in their Slack team,
        # Slack returns a temporary authorization code that we'll exchange for
        # an OAuth token using the oauth.access endpoint
        auth_response = self.client.api_call(
                                "oauth.access",
                                client_id=self.client_id,
                                client_secret=self.client_secret,
                                code=code
                                )
        # To keep track of authorized teams and their associated OAuth tokens,
        # we will save the team ID and bot tokens to the global
        # authed_teams object
        team_name = auth_response["team_name"]
        team_id = auth_response["team_id"]
        authentication = {"team_name": team_name,
                          "team_id": team_id,
                          "auth_token": auth_response["access_token"],
                          "scope": auth_response["scope"],
                          "bot_user_id":
                          auth_response["bot"]["bot_user_id"],
                          "bot_token":
                          auth_response["bot"]["bot_access_token"]}
        # Then we'll reconnect to the Slack Client with the correct team's
        # bot token
        self.client = SlackClient(authentication["bot_token"])

        # Return the authentication for storage
        return authentication
