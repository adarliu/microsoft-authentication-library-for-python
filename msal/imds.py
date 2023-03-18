# Copyright (c) Microsoft Corporation.
# All rights reserved.
#
# This code is licensed under the MIT License.
import json
import logging
import os
import socket
import time
try:  # Python 2
    from urlparse import urlparse
except:  # Python 3
    from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def _scope_to_resource(scope):  # This is an experimental reasonable-effort approach
    u = urlparse(scope)
    if u.scheme:
        return "{}://{}".format(u.scheme, u.netloc)
    return scope  # There is no much else we can do here


def _obtain_token(http_client, resource, client_id=None, object_id=None, mi_res_id=None):
    if ("IDENTITY_ENDPOINT" in os.environ and "IDENTITY_HEADER" in os.environ
            and "IDENTITY_SERVER_THUMBPRINT" in os.environ
    ):
        if client_id or object_id or mi_res_id:
            logger.debug(
                "Ignoring client_id/object_id/mi_res_id. "
                "Managed Identity in Service Fabric is configured in the cluster, "
                "not during runtime. See also "
                "https://learn.microsoft.com/en-us/azure/service-fabric/configure-existing-cluster-enable-managed-identity-token-service")
        return _obtain_token_on_service_fabric(
            http_client, os.environ["IDENTITY_ENDPOINT"], os.environ["IDENTITY_HEADER"],
            os.environ["IDENTITY_SERVER_THUMBPRINT"], resource)
    if "IDENTITY_ENDPOINT" in os.environ and "IDENTITY_HEADER" in os.environ:
        return _obtain_token_on_app_service(
            http_client, os.environ["IDENTITY_ENDPOINT"], os.environ["IDENTITY_HEADER"],
            resource, client_id=client_id, object_id=object_id, mi_res_id=mi_res_id)
    return _obtain_token_on_azure_vm(
        http_client,
        resource, client_id=client_id, object_id=object_id, mi_res_id=mi_res_id)


def _obtain_token_on_azure_vm(http_client, resource,
    client_id=None, object_id=None, mi_res_id=None,
):
    # Based on https://docs.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/how-to-use-vm-token#get-a-token-using-http
    logger.debug("Obtaining token via managed identity on Azure VM")
    params = {
        "api-version": "2018-02-01",
        "resource": resource,
        }
    if client_id:
        params["client_id"] = client_id
    if object_id:
        params["object_id"] = object_id
    if mi_res_id:
        params["mi_res_id"] = mi_res_id
    resp = http_client.get(
        "http://169.254.169.254/metadata/identity/oauth2/token",
        params=params,
        headers={"Metadata": "true"},
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_in"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": int(payload["expires_in"]),
                "resource": payload.get("resource"),
                "token_type": payload.get("token_type", "Bearer"),
                }
        return payload  # Typically an error, but it is undefined in the doc above
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise

def _obtain_token_on_app_service(http_client, endpoint, identity_header, resource,
    client_id=None, object_id=None, mi_res_id=None,
):
    """Obtains token for
    `App Service <https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity?tabs=portal%2Chttp#rest-endpoint-reference>`_,
    Azure Functions, and Azure Automation.
    """
    # Prerequisite: Create your app service https://docs.microsoft.com/en-us/azure/app-service/quickstart-python
    # Assign it a managed identity https://docs.microsoft.com/en-us/azure/app-service/overview-managed-identity?tabs=portal%2Chttp
    # SSH into your container for testing https://docs.microsoft.com/en-us/azure/app-service/configure-linux-open-ssh-session
    logger.debug("Obtaining token via managed identity on Azure App Service")
    params = {
        "api-version": "2019-08-01",
        "resource": resource,
        }
    if client_id:
        params["client_id"] = client_id
    if object_id:
        params["object_id"] = object_id
    if mi_res_id:
        params["mi_res_id"] = mi_res_id
    resp = http_client.get(
        endpoint,
        params=params,
        headers={
            "X-IDENTITY-HEADER": identity_header,
            "Metadata": "true",  # Unnecessary yet harmless for App Service,
            # It will be needed by Azure Automation
            # https://docs.microsoft.com/en-us/azure/automation/enable-managed-identity-for-automation#get-access-token-for-system-assigned-managed-identity-using-http-get
            },
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_on"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": int(payload["expires_on"]) - int(time.time()),
                "resource": payload.get("resource"),
                "token_type": payload.get("token_type", "Bearer"),
                }
        return {
            "error": "invalid_scope",  # Empirically, wrong resource ends up with a vague statusCode=500
            "error_description": "{}, {}".format(
                payload.get("statusCode"), payload.get("message")),
            }
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise


def _obtain_token_on_service_fabric(
    http_client, endpoint, identity_header, server_thumbprint, resource,
):
    """Obtains token for
    `Service Fabric <https://learn.microsoft.com/en-us/azure/service-fabric/>`_
    """
    # Deployment https://learn.microsoft.com/en-us/azure/service-fabric/service-fabric-get-started-containers-linux
    # See also https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/tests/managed-identity-live/service-fabric/service_fabric.md
    # Protocol https://learn.microsoft.com/en-us/azure/service-fabric/how-to-managed-identity-service-fabric-app-code#acquiring-an-access-token-using-rest-api
    logger.debug("Obtaining token via managed identity on Azure Service Fabric")
    resp = http_client.get(
        endpoint,
        params={"api-version": "2019-07-01-preview", "resource": resource},
        headers={"Secret": identity_header},
        )
    try:
        payload = json.loads(resp.text)
        if payload.get("access_token") and payload.get("expires_on"):
            return {  # Normalizing the payload into OAuth2 format
                "access_token": payload["access_token"],
                "expires_in": payload["expires_on"] - int(time.time()),
                "resource": payload.get("resource"),
                "token_type": payload["token_type"],
                }
        error = payload.get("error", {})  # https://learn.microsoft.com/en-us/azure/service-fabric/how-to-managed-identity-service-fabric-app-code#error-handling
        error_mapping = {  # Map Service Fabric errors into OAuth2 errors  https://www.rfc-editor.org/rfc/rfc6749#section-5.2
            "SecretHeaderNotFound": "unauthorized_client",
            "ManagedIdentityNotFound": "invalid_client",
            "ArgumentNullOrEmpty": "invalid_scope",
            }
        return {
            "error": error_mapping.get(payload["error"]["code"], "invalid_request"),
            "error_description": resp.text,
            }
    except ValueError:
        logger.debug("IMDS emits unexpected payload: %s", resp.text)
        raise



class ManagedIdentity(object):
    _instance, _tenant = socket.getfqdn(), "managed_identity"  # Placeholders

    def __init__(self, http_client,
        client_id=None, object_id=None, mi_res_id=None,
        token_cache=None,
    ):
        """Create a managed identity object.

        :param http_client:
            An http client object. For example, you can use `requests.Session()`.

        :param str client_id:
            Optional.
            It accepts the Client ID (NOT the Object ID) of your user-assigned managed identity.
            If it is None, it means to use a system-assigned managed identity.

        :param token_cache:
            Optional. It accepts a :class:`msal.TokenCache` instance to store tokens.
        """
        if len(list(filter(bool, [client_id, object_id, mi_res_id]))) > 1:
            raise ValueError("You can use up to one of these: client_id, object_id, mi_res_id")
        self._http_client = http_client
        self._client_id = client_id
        self._object_id = object_id
        self._mi_res_id = mi_res_id
        self._token_cache = token_cache

    def acquire_token(self, resource=None):
        if not resource:
            raise ValueError(
                "The resource parameter is currently required. "
                "It is only declared as optional in method signature, "
                "in case we want to support scope parameter in the future.")
        access_token_from_cache = None
        client_id_in_cache = (
            self._client_id or self._object_id or self._mi_res_id
            or "SYSTEM_ASSIGNED_MANAGED_IDENTITY")
        if self._token_cache:
            matches = self._token_cache.find(
                self._token_cache.CredentialType.ACCESS_TOKEN,
                target=[resource],
                query=dict(
                    client_id=client_id_in_cache,
                    environment=self._instance,
                    realm=self._tenant,
                    home_account_id=None,
                ),
            )
            now = time.time()
            for entry in matches:
                expires_in = int(entry["expires_on"]) - now
                if expires_in < 5*60:  # Then consider it expired
                    continue  # Removal is not necessary, it will be overwritten
                logger.debug("Cache hit an AT")
                access_token_from_cache = {  # Mimic a real response
                    "access_token": entry["secret"],
                    "token_type": entry.get("token_type", "Bearer"),
                    "expires_in": int(expires_in),  # OAuth2 specs defines it as int
                }
                if "refresh_on" in entry and int(entry["refresh_on"]) < now:  # aging
                    break  # With a fallback in hand, we break here to go refresh
                return access_token_from_cache  # It is still good as new
        result = _obtain_token(
            self._http_client,
            resource,
            client_id=self._client_id,
            object_id=self._object_id,
            mi_res_id=self._mi_res_id,
            )
        if self._token_cache and "access_token" in result:
            self._token_cache.add(dict(
                client_id=client_id_in_cache,
                scope=[resource],
                token_endpoint="https://{}/{}".format(self._instance, self._tenant),
                response=result,
                params={},
                data={},
                #grant_type="placeholder",
            ))
            return result
        return access_token_from_cache or result

