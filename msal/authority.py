import json
try:
    from urllib.parse import urlparse
except ImportError:  # Fall back to Python 2
    from urlparse import urlparse
import logging

from .exceptions import MsalServiceError


logger = logging.getLogger(__name__)

# Endpoints were copied from here
# https://docs.microsoft.com/en-us/azure/active-directory/develop/authentication-national-cloud#azure-ad-authentication-endpoints
AZURE_US_GOVERNMENT = "login.microsoftonline.us"
AZURE_CHINA = "login.chinacloudapi.cn"
AZURE_PUBLIC = "login.microsoftonline.com"

WORLD_WIDE = 'login.microsoftonline.com'  # There was an alias login.windows.net
WELL_KNOWN_AUTHORITY_HOSTS = set([
    WORLD_WIDE,
    AZURE_CHINA,
    'login-us.microsoftonline.com',
    AZURE_US_GOVERNMENT,
    ])
WELL_KNOWN_B2C_HOSTS = [
    "b2clogin.com",
    "b2clogin.cn",
    "b2clogin.us",
    "b2clogin.de",
    ]


class AuthorityBuilder(object):
    def __init__(self, instance, tenant):
        """A helper to save caller from doing string concatenation.

        Usage is documented in :func:`application.ClientApplication.__init__`.
        """
        self._instance = instance.rstrip("/")
        self._tenant = tenant.strip("/")

    def __str__(self):
        return "https://{}/{}".format(self._instance, self._tenant)


class Authority(object):
    """This class represents an (already-validated) authority.

    Once constructed, it contains members named "*_endpoint" for this instance.
    TODO: It will also cache the previously-validated authority instances.
    """
    _domains_without_user_realm_discovery = set([])

    def __init__(
            self, authority_url, http_client,
            validate_authority=True,
            instance_discovery=None,
            ):
        """Creates an authority instance, and also validates it.

        :param validate_authority:
            The Authority validation process actually checks two parts:
            instance (a.k.a. host) and tenant. We always do a tenant discovery.
            This parameter only controls whether an instance discovery will be
            performed.
        """
        # :param instance_discovery:
        #    By default, the known-to-Microsoft validation will use an
        #    instance discovery endpoint located at ``login.microsoftonline.com``.
        #    You can customize the endpoint by providing a url as a string.
        #    Or you can turn this behavior off by passing in a False here.
        self._http_client = http_client
        if isinstance(authority_url, AuthorityBuilder):
            authority_url = str(authority_url)
        authority, self.instance, tenant = canonicalize(authority_url)
        self.is_adfs = tenant.lower() == 'adfs'
        parts = authority.path.split('/')
        self._is_b2c = any(
            self.instance.endswith("." + d) for d in WELL_KNOWN_B2C_HOSTS
            ) or (len(parts) == 3 and parts[2].lower().startswith("b2c_"))
        self._is_known_to_developer = self.is_adfs or self._is_b2c or not validate_authority
        is_known_to_microsoft = self.instance in WELL_KNOWN_AUTHORITY_HOSTS
        instance_discovery_endpoint = 'https://{}/common/discovery/instance'.format(  # Note: This URL seemingly returns V1 endpoint only
            WORLD_WIDE  # Historically using WORLD_WIDE. Could use self.instance too
                # See https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/4.0.0/src/Microsoft.Identity.Client/Instance/AadInstanceDiscovery.cs#L101-L103
                # and https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/4.0.0/src/Microsoft.Identity.Client/Instance/AadAuthority.cs#L19-L33
            ) if instance_discovery in (None, True) else instance_discovery
        if instance_discovery_endpoint and not (
                is_known_to_microsoft or self._is_known_to_developer):
            payload = _instance_discovery(
                "https://{}{}/oauth2/v2.0/authorize".format(
                    self.instance, authority.path),
                self._http_client,
                instance_discovery_endpoint)
            if payload.get("error") == "invalid_instance":
                raise ValueError(
                    "invalid_instance: "
                    "The authority you provided, %s, is not whitelisted. "
                    "If it is indeed your legit customized domain name, "
                    "you can turn off this check by passing in "
                    "validate_authority=False"
                    % authority_url)
            tenant_discovery_endpoint = payload['tenant_discovery_endpoint']
        else:
            tenant_discovery_endpoint = (
                'https://{}:{}{}{}/.well-known/openid-configuration'.format(
                    self.instance,
                    443 if authority.port is None else authority.port,
                    authority.path,  # In B2C scenario, it is "/tenant/policy"
                    "" if tenant == "adfs" else "/v2.0" # the AAD v2 endpoint
                    ))
        try:
            openid_config = tenant_discovery(
                tenant_discovery_endpoint,
                self._http_client)
        except ValueError:
            raise ValueError(
                "Unable to get authority configuration for {}. "
                "Authority would typically be in a format of "
                "https://login.microsoftonline.com/your_tenant "
                "Also please double check your tenant name or GUID is correct.".format(
                authority_url))
        logger.debug("openid_config = %s", openid_config)
        self.authorization_endpoint = openid_config['authorization_endpoint']
        self.token_endpoint = openid_config['token_endpoint']
        self.device_authorization_endpoint = openid_config.get('device_authorization_endpoint')
        _, _, self.tenant = canonicalize(self.token_endpoint)  # Usually a GUID

    def user_realm_discovery(self, username, correlation_id=None, response=None):
        # It will typically return a dict containing "ver", "account_type",
        # "federation_protocol", "cloud_audience_urn",
        # "federation_metadata_url", "federation_active_auth_url", etc.
        if self.instance not in self.__class__._domains_without_user_realm_discovery:
            resp = response or self._http_client.get(
                "https://{netloc}/common/userrealm/{username}?api-version=1.0".format(
                    netloc=self.instance, username=username),
                headers={'Accept': 'application/json',
                         'client-request-id': correlation_id},)
            if resp.status_code != 404:
                resp.raise_for_status()
                return json.loads(resp.text)
            self.__class__._domains_without_user_realm_discovery.add(self.instance)
        return {}  # This can guide the caller to fall back normal ROPC flow


def canonicalize(authority_url):
    # Returns (url_parsed_result, hostname_in_lowercase, tenant)
    authority = urlparse(authority_url)
    parts = authority.path.split("/")
    if authority.scheme != "https" or len(parts) < 2 or not parts[1]:
        raise ValueError(
            "Your given address (%s) should consist of "
            "an https url with a minimum of one segment in a path: e.g. "
            "https://login.microsoftonline.com/<tenant> "
            "or https://<tenant_name>.b2clogin.com/<tenant_name>.onmicrosoft.com/policy"
            % authority_url)
    return authority, authority.hostname, parts[1]

def _instance_discovery(url, http_client, instance_discovery_endpoint, **kwargs):
    resp = http_client.get(
        instance_discovery_endpoint,
        params={'authorization_endpoint': url, 'api-version': '1.0'},
        **kwargs)
    return json.loads(resp.text)

def tenant_discovery(tenant_discovery_endpoint, http_client, **kwargs):
    # Returns Openid Configuration
    resp = http_client.get(tenant_discovery_endpoint, **kwargs)
    if resp.status_code == 200:
        return json.loads(resp.text)  # It could raise ValueError
    if 400 <= resp.status_code < 500:
        # Nonexist tenant would hit this path
        # e.g. https://login.microsoftonline.com/nonexist_tenant/v2.0/.well-known/openid-configuration
        raise ValueError("OIDC Discovery failed on {}. HTTP status: {}, Error: {}".format(
            tenant_discovery_endpoint,
            resp.status_code,
            resp.text,  # Expose it as-is b/c OIDC defines no error response format
            ))
    # Transient network error would hit this path
    resp.raise_for_status()
    raise RuntimeError(  # A fallback here, in case resp.raise_for_status() is no-op
        "Unable to complete OIDC Discovery: %d, %s" % (resp.status_code, resp.text))

