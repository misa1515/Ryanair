"""Ryanair Coordinator."""
from datetime import timedelta
import logging
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONTENT_TYPE_JSON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
import re
from aztec_code_generator import AztecCode
from .const import (
    HOST,
    USER_PROFILE,
    ORDERS,
    V,
    CUSTOMERS,
    ACCOUNTS,
    ACCOUNT_LOGIN,
    CONF_DEVICE_FINGERPRINT,
    CONF_POLICY_AGREED,
    MFA_CODE,
    ACCOUNT_VERIFICATION,
    DEVICE_VERIFICATION,
    MFA_TOKEN,
    CONF_AUTH_TOKEN,
    PROFILE,
    CUSTOMER_ID,
    TOKEN,
    REMEMBER_ME_TOKEN,
    DETAILS,
    PERSISTENCE,
    REMEMBER_ME,
    X_REMEMBER_ME_TOKEN,
    ACCESS_DENIED,
    CAUSE,
    NOT_AUTHENTICATED,
    CLIENT_ERROR,
    TYPE,
    BOARDING_PASS_URL,
    EMAIL,
    RECORD_LOCATOR
)
from .errors import RyanairError, InvalidAuth, APIRatelimitExceeded, UnknownError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util.json import JsonArrayType, load_json_object
from homeassistant.helpers.json import save_json

_LOGGER = logging.getLogger(__name__)

USER_PROFILE_URL = HOST + USER_PROFILE + V
ORDERS_URL = HOST + ORDERS + V


async def rememberMeToken(self, data):
    rememberMeTokenResp = await self.session.request(
        method="GET",
        url=USER_PROFILE_URL
        + ACCOUNTS
        + "/"
        + data[CUSTOMER_ID]
        + "/"
        + REMEMBER_ME_TOKEN,
        headers={
            CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
            CONF_AUTH_TOKEN: data[TOKEN],
        },
    )
    rememberMeTokenResponse = await rememberMeTokenResp.json()

    return rememberMeTokenResponse


async def refreshToken(self, data):
    rememberMeResp = await self.session.request(
        method="GET",
        url=USER_PROFILE_URL + ACCOUNTS + "/" + REMEMBER_ME,
        headers={
            CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
            X_REMEMBER_ME_TOKEN: data[X_REMEMBER_ME_TOKEN],
        },
    )
    rememberMeResponse = await rememberMeResp.json()

    ryanairData = {
        CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
        CUSTOMER_ID: data[CUSTOMER_ID],
        TOKEN: rememberMeResponse[TOKEN],
        X_REMEMBER_ME_TOKEN: data[X_REMEMBER_ME_TOKEN]
    }

    rememberMeTokenResp = await rememberMeToken(self, ryanairData)

    ryanairData = {
        CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
        CUSTOMER_ID: data[CUSTOMER_ID],
        TOKEN: rememberMeResponse[TOKEN],
        X_REMEMBER_ME_TOKEN: rememberMeTokenResp[TOKEN]
    }

    save_json(self.hass.config.path(PERSISTENCE), ryanairData)
    return ryanairData


async def getFlights(self, data):
    resp = await self.session.request(
        method="GET",
        url=ORDERS_URL + ORDERS + data[CUSTOMER_ID] + "/" + DETAILS,
        headers={
            "Content-Type": CONTENT_TYPE_JSON,
            CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
            CONF_AUTH_TOKEN: data[TOKEN],
        },
    )
    body = await resp.json()

    return body


async def getUserProfile(self, data):
    resp = await self.session.request(
        method="GET",
        url=USER_PROFILE_URL + CUSTOMERS + "/" +
        data[CUSTOMER_ID] + "/" + PROFILE,
        headers={
            "Content-Type": CONTENT_TYPE_JSON,
            CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
            CONF_AUTH_TOKEN: data[TOKEN],
        },
    )
    body = await resp.json()

    return body


async def getBoardingPasses(self, data, headers):
    resp = await self.session.request(
        method="POST",
        url=BOARDING_PASS_URL,
        headers={
            "Content-Type": CONTENT_TYPE_JSON,
            CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
            CONF_AUTH_TOKEN: data[TOKEN],
        },
        json={
            EMAIL: headers[EMAIL],
            RECORD_LOCATOR: headers[RECORD_LOCATOR]
        }
    )
    body = await resp.json()

    return body


class RyanairBoardingPassCoordinator(DataUpdateCoordinator):
    """Boarding Pass Coordinator"""

    def __init__(self, hass: HomeAssistant, session, data) -> None:
        """Initialize coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Ryanair",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=300),
        )
        self.email = data[EMAIL]
        self.record_locator = data[RECORD_LOCATOR]
        self.session = session

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            data = load_json_object(self.hass.config.path(PERSISTENCE))

            if X_REMEMBER_ME_TOKEN not in data:
                rememberMeTokenResp = await rememberMeToken(self, data)

                data = {
                    CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
                    CUSTOMER_ID: data[CUSTOMER_ID],
                    TOKEN: data[TOKEN],
                    X_REMEMBER_ME_TOKEN: rememberMeTokenResp[TOKEN]
                }
                save_json(self.hass.config.path(PERSISTENCE), data)

            headers = {
                EMAIL: self.email,
                RECORD_LOCATOR: self.record_locator
            }

            body = await getBoardingPasses(self, data, headers)

            if (ACCESS_DENIED in body and body[CAUSE] == NOT_AUTHENTICATED) or (
                TYPE in body and body[TYPE] == CLIENT_ERROR
            ):
                refreshedData = await refreshToken(self, data)

                headers = {
                    EMAIL: self.email,
                    RECORD_LOCATOR: self.record_locator
                }

                body = await getBoardingPasses(self, refreshedData, headers)

            for boardingPass in body:
                aztec_code = AztecCode(boardingPass['barcode'])
                fileName = re.sub(
                    "[\W_]", "", boardingPass['barcode']) + ".png"

                aztec_code.save(
                    "homeassistant/components/ryanair/" + fileName, module_size=16)

        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        except RyanairError as err:
            raise UpdateFailed(str(err)) from err
        except ValueError as err:
            err_str = str(err)

            if "Invalid authentication credentials" in err_str:
                raise InvalidAuth from err
            if "API rate limit exceeded." in err_str:
                raise APIRatelimitExceeded from err

            _LOGGER.exception("Unexpected exception")
            raise UnknownError from err

        return body


class RyanairFlightsCoordinator(DataUpdateCoordinator):
    """Flights Coordinator"""

    def __init__(self, hass: HomeAssistant, session, data) -> None:
        """Initialize coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Ryanair",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=300),
        )

        self.session = session

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            data = load_json_object(self.hass.config.path(PERSISTENCE))

            if X_REMEMBER_ME_TOKEN not in data:
                rememberMeTokenResp = await rememberMeToken(self, data)

                data = {
                    CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
                    CUSTOMER_ID: data[CUSTOMER_ID],
                    TOKEN: data[TOKEN],
                    X_REMEMBER_ME_TOKEN: rememberMeTokenResp[TOKEN]
                }
                save_json(self.hass.config.path(PERSISTENCE), data)

            body = await getFlights(self, data)

            if (ACCESS_DENIED in body and body[CAUSE] == NOT_AUTHENTICATED) or (
                TYPE in body and body[TYPE] == CLIENT_ERROR
            ):
                refreshedData = await refreshToken(self, data)

                body = await getFlights(self, refreshedData)

        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        except RyanairError as err:
            raise UpdateFailed(str(err)) from err
        except ValueError as err:
            err_str = str(err)

            if "Invalid authentication credentials" in err_str:
                raise InvalidAuth from err
            if "API rate limit exceeded." in err_str:
                raise APIRatelimitExceeded from err

            _LOGGER.exception("Unexpected exception")
            raise UnknownError from err

        return body


class RyanairProfileCoordinator(DataUpdateCoordinator):
    """User Profile Coordinator"""

    def __init__(self, hass: HomeAssistant, session, data) -> None:
        """Initialize coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Ryanair",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=300),
        )

        self.session = session

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            data = load_json_object(self.hass.config.path(PERSISTENCE))

            if X_REMEMBER_ME_TOKEN not in data:
                rememberMeTokenResp = await rememberMeToken(self, data)

                data = {
                    CONF_DEVICE_FINGERPRINT: data[CONF_DEVICE_FINGERPRINT],
                    CUSTOMER_ID: data[CUSTOMER_ID],
                    TOKEN: data[TOKEN],
                    X_REMEMBER_ME_TOKEN: rememberMeTokenResp[TOKEN]
                }
                save_json(self.hass.config.path(PERSISTENCE), data)

            body = await getUserProfile(self, data)

            if (ACCESS_DENIED in body and body[CAUSE] == NOT_AUTHENTICATED) or (
                TYPE in body and body[TYPE] == CLIENT_ERROR
            ):
                refreshedData = await refreshToken(self, data)

                body = await getUserProfile(self, refreshedData)

        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        except RyanairError as err:
            raise UpdateFailed(str(err)) from err
        except ValueError as err:
            err_str = str(err)

            if "Invalid authentication credentials" in err_str:
                raise InvalidAuth from err
            if "API rate limit exceeded." in err_str:
                raise APIRatelimitExceeded from err

            _LOGGER.exception("Unexpected exception")
            raise UnknownError from err

        return body


class RyanairMfaCoordinator(DataUpdateCoordinator):
    """MFA coordinator."""

    def __init__(self, hass: HomeAssistant, session, data) -> None:
        """Initialize coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Ryanair",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=300),
        )

        self.session = session
        self.mfaCode = data[MFA_CODE]
        self.mfaToken = data[MFA_TOKEN]
        self.fingerprint = data[CONF_DEVICE_FINGERPRINT]

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        try:
            resp = await self.session.request(
                method="PUT",
                url=USER_PROFILE_URL + ACCOUNT_VERIFICATION + "/" + DEVICE_VERIFICATION,
                headers={
                    "Content-Type": CONTENT_TYPE_JSON,
                    CONF_DEVICE_FINGERPRINT: self.fingerprint,
                },
                json={MFA_CODE: self.mfaCode, MFA_TOKEN: self.mfaToken},
            )
            body = await resp.json()
            # session expired
            # {'access-denied': True, 'message': 'Full authentication is required to access this resource.', 'cause': 'NOT AUTHENTICATED'}

        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        except RyanairError as err:
            raise UpdateFailed(str(err)) from err
        except ValueError as err:
            err_str = str(err)

            if "Invalid authentication credentials" in err_str:
                raise InvalidAuth from err
            if "API rate limit exceeded." in err_str:
                raise APIRatelimitExceeded from err

            _LOGGER.exception("Unexpected exception")
            raise UnknownError from err

        return body


class RyanairCoordinator(DataUpdateCoordinator):
    """Data coordinator."""

    def __init__(self, hass: HomeAssistant, session, data) -> None:
        """Initialize coordinator."""

        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Ryanair",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=300),
        )

        self.session = session
        self.email = data[CONF_EMAIL]
        self.password = data[CONF_PASSWORD]
        self.fingerprint = data[CONF_DEVICE_FINGERPRINT]

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            resp = await self.session.request(
                method="POST",
                url=USER_PROFILE_URL + ACCOUNT_LOGIN,
                headers={
                    "Content-Type": CONTENT_TYPE_JSON,
                    CONF_DEVICE_FINGERPRINT: self.fingerprint,
                },
                json={
                    CONF_EMAIL: self.email,
                    CONF_PASSWORD: self.password,
                    CONF_POLICY_AGREED: "true",
                },
            )
            body = await resp.json()

        except InvalidAuth as err:
            raise ConfigEntryAuthFailed from err
        except RyanairError as err:
            raise UpdateFailed(str(err)) from err
        except ValueError as err:
            err_str = str(err)

            if "Invalid authentication credentials" in err_str:
                raise InvalidAuth from err
            if "API rate limit exceeded." in err_str:
                raise APIRatelimitExceeded from err

            _LOGGER.exception("Unexpected exception")
            raise UnknownError from err

        return body
