"""Config flow for BOM."""
import logging
from typing import Any, Dict, List, Optional

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import callback

from .const import (
    CONF_FORECASTS_BASENAME,
    CONF_FORECASTS_CREATE,
    CONF_FORECASTS_DAYS,
    CONF_FORECASTS_MONITORED,
    CONF_OBSERVATIONS_BASENAME,
    CONF_OBSERVATIONS_CREATE,
    CONF_OBSERVATIONS_MONITORED,
    CONF_WARNINGS_BASENAME,
    CONF_WARNINGS_CREATE,
    CONF_WEATHER_NAME,
    DOMAIN,
    OBSERVATION_SENSOR_TYPES,
    FORECAST_SENSOR_TYPES,
    URL_BASE,
)
from .PyBoM.collector import Collector, search_locations

_LOGGER = logging.getLogger(__name__)

# New constants for location search
CONF_LOCATION_METHOD = "location_method"
CONF_LOCATION_SEARCH = "location_search"
CONF_LOCATION_SELECTION = "location_selection"

LOCATION_METHOD_LATLON = "latlon"
LOCATION_METHOD_SEARCH = "search"

URL_LOCATION_SEARCH = "locations?search="


async def validate_location(hass, user_input):
    """Validate location data and create collector."""
    errors = {}
    collector = None
    try:
        # Create the collector object with the given long. and lat.
        collector = Collector(
            user_input[CONF_LATITUDE],
            user_input[CONF_LONGITUDE],
        )

        # Check if location is valid
        await collector.get_locations_data()
        if collector.locations_data is None:
            _LOGGER.debug("Unsupported Lat/Lon")
            errors["base"] = "bad_location"
        else:
            # Populate observations and daily forecasts data
            await collector.async_update()

    except Exception:
        _LOGGER.exception("Unexpected exception")
        errors["base"] = "unknown"

    return errors, collector


def get_observations_schema(collector, monitored_default=None):
    """Get schema for observations monitored step."""
    monitored = {sensor.key: sensor.name for sensor in OBSERVATION_SENSOR_TYPES}
    
    return vol.Schema(
        {
            vol.Required(
                CONF_OBSERVATIONS_BASENAME,
                default=collector.observations_data["data"]["station"]["name"],
            ): str,
            vol.Required(
                CONF_OBSERVATIONS_MONITORED, 
                default=monitored_default
            ): cv.multi_select(monitored),
        }
    )


def get_forecasts_schema(collector, monitored_default=None, days_default=0):
    """Get schema for forecasts monitored step."""
    monitored = {sensor.key: sensor.name for sensor in FORECAST_SENSOR_TYPES}
    
    return vol.Schema(
        {
            vol.Required(
                CONF_FORECASTS_BASENAME,
                default=collector.locations_data["data"]["name"],
            ): str,
            vol.Required(
                CONF_FORECASTS_MONITORED,
                default=monitored_default
            ): cv.multi_select(monitored),
            vol.Required(
                CONF_FORECASTS_DAYS,
                default=days_default
            ): vol.All(vol.Coerce(int), vol.Range(0, 7)),
        }
    )


def get_warnings_schema(collector):
    """Get schema for warnings basename step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_WARNINGS_BASENAME,
                default=collector.locations_data["data"]["name"],
            ): str,
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BOM."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the config flow."""
        super().__init__()
        self.data = {}
        self.collector = None
        self.locations = []
        # Create a collector with dummy coordinates for API operations
        # We'll update with real coordinates once we have them
        self.search_collector = Collector(0, 0)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return BomOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step to choose location input method."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_LOCATION_METHOD, default=LOCATION_METHOD_LATLON): vol.In(
                    {
                        LOCATION_METHOD_LATLON: "Enter latitude/longitude",
                        LOCATION_METHOD_SEARCH: "Search for a location",
                    }
                ),
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=data_schema)

        if user_input[CONF_LOCATION_METHOD] == LOCATION_METHOD_LATLON:
            return await self.async_step_latlon()
        else:
            return await self.async_step_location_search()

    async def async_step_latlon(self, user_input=None):
        """Handle the latitude/longitude input step."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_LATITUDE, default=self.hass.config.latitude): float,
                vol.Required(CONF_LONGITUDE, default=self.hass.config.longitude): float,
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="latlon", data_schema=data_schema)

        errors, collector = await validate_location(self.hass, user_input)
        
        if errors:
            return self.async_show_form(
                step_id="latlon", data_schema=data_schema, errors=errors
            )
            
        # Store data and collector for future steps
        self.data.update(user_input)
        self.collector = collector
        
        return await self.async_step_weather_name()

    async def async_step_location_search(self, user_input=None):
        """Handle the location search step."""
        data_schema = vol.Schema({
            vol.Required(CONF_LOCATION_SEARCH): str,
        })

        if user_input is None:
            return self.async_show_form(step_id="location_search", data_schema=data_schema)

        search_term = user_input[CONF_LOCATION_SEARCH]
        errors = {}
        locations = []

        try:
            # Use the collector's fetch_with_retry method for consistency
            import aiohttp
            headers = {"User-Agent": "MakeThisAPIOpenSource/1.0.0"}
            
            async with aiohttp.ClientSession(headers=headers) as session:
                search_url = f"{URL_BASE}{URL_LOCATION_SEARCH}{search_term}"
                data = await self.search_collector._fetch_with_retry(
                    session, search_url, "location_search"
                )
                
                if data and "data" in data and data["data"]:
                    locations = data["data"]
                else:
                    errors["base"] = "no_locations_found"
        except Exception:
            _LOGGER.exception("Unexpected exception during location search")
            errors["base"] = "unknown"
        
        if errors:
            return self.async_show_form(
                step_id="location_search", data_schema=data_schema, errors=errors
            )
        
        if not locations:
            errors["base"] = "no_locations_found"
            return self.async_show_form(
                step_id="location_search", data_schema=data_schema, errors=errors
            )
        
        # Store locations for selection step
        self.locations = locations
        return await self.async_step_location_selection()

    async def async_step_location_selection(self, user_input=None):
        """Handle location selection from search results."""
        # Create options with name, state and postcode
        options = {}
        for location in self.locations:
            geohash = location.get("geohash", "")
            name = location.get("name", "")
            state = location.get("state", "")
            postcode = location.get("postcode", "")
            display_name = f"{name}, {state} {postcode}"
            options[geohash] = display_name
        
        data_schema = vol.Schema({
            vol.Required(CONF_LOCATION_SELECTION): vol.In(options)
        })

        if user_input is None:
            return self.async_show_form(step_id="location_selection", data_schema=data_schema)

        # Get the selected location
        selected_geohash = user_input[CONF_LOCATION_SELECTION]
        selected_location = next(
            (loc for loc in self.locations if loc.get("geohash") == selected_geohash),
            None
        )
        
        if not selected_location:
            return self.async_abort(reason="location_not_found")
        
        # Extract lat/lon from the selected location
        latitude = selected_location.get("latitude")
        longitude = selected_location.get("longitude")
        
        if latitude is None or longitude is None:
            return self.async_abort(reason="invalid_location_data")
        
        # Create location data
        location_data = {
            CONF_LATITUDE: latitude,
            CONF_LONGITUDE: longitude,
        }
        
        # Validate the location
        errors, collector = await validate_location(self.hass, location_data)
        
        if errors:
            return self.async_show_form(
                step_id="location_selection", data_schema=data_schema, errors=errors
            )
        
        # Store data and collector for future steps
        self.data.update(location_data)
        self.collector = collector
        
        return await self.async_step_weather_name()

    async def async_step_weather_name(self, user_input=None):
        """Handle the locations step."""
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_WEATHER_NAME,
                    default=self.collector.locations_data["data"]["name"],
                ): str,
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="weather_name", data_schema=data_schema)

        errors = {}
        try:
            self.data.update(user_input)
            return await self.async_step_sensors_create()
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="weather_name", data_schema=data_schema, errors=errors
        )

    async def async_step_sensors_create(self, user_input=None):
        """Handle the observations step."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_OBSERVATIONS_CREATE, default=True): bool,
                vol.Required(CONF_FORECASTS_CREATE, default=True): bool,
                vol.Required(CONF_WARNINGS_CREATE, default=True): bool,
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="sensors_create", data_schema=data_schema)

        errors = {}
        try:
            self.data.update(user_input)
            
            # Determine next step based on selections
            if self.data[CONF_OBSERVATIONS_CREATE]:
                return await self.async_step_observations_monitored()
            elif self.data[CONF_FORECASTS_CREATE]:
                return await self.async_step_forecasts_monitored()
            elif self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            else:
                return self.async_create_entry(
                    title=self.collector.locations_data["data"]["name"],
                    data=self.data,
                )
                
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="sensors_create", data_schema=data_schema, errors=errors
        )

    async def async_step_observations_monitored(self, user_input=None):
        """Handle the observations monitored step."""
        data_schema = get_observations_schema(self.collector)

        if user_input is None:
            return self.async_show_form(
                step_id="observations_monitored", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            
            # Determine next step based on selections
            if self.data[CONF_FORECASTS_CREATE]:
                return await self.async_step_forecasts_monitored()
            elif self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            else:
                return self.async_create_entry(
                    title=self.collector.locations_data["data"]["name"],
                    data=self.data,
                )
                
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="observations_monitored", data_schema=data_schema, errors=errors
        )

    async def async_step_forecasts_monitored(self, user_input=None):
        """Handle the forecasts monitored step."""
        data_schema = get_forecasts_schema(self.collector)

        if user_input is None:
            return self.async_show_form(
                step_id="forecasts_monitored", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            
            if self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            
            return self.async_create_entry(
                title=self.collector.locations_data["data"]["name"],
                data=self.data
            )
            
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="forecasts_monitored", data_schema=data_schema, errors=errors
        )

    async def async_step_warnings_basename(self, user_input=None):
        """Handle the warnings basename step."""
        data_schema = get_warnings_schema(self.collector)

        if user_input is None:
            return self.async_show_form(
                step_id="warnings_basename", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            return self.async_create_entry(
                title=self.collector.locations_data["data"]["name"],
                data=self.data
            )
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="warnings_basename", data_schema=data_schema, errors=errors
        )


class BomOptionsFlow(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        super().__init__()
        self.config_entry = config_entry
        self.data = {}
        self.collector = None
        self.locations = []
        # Create a collector with dummy coordinates for API operations
        # We'll update with real coordinates once we have them
        self.search_collector = Collector(0, 0)

    def get_default_value(self, key, default_value=None):
        """Get default value considering config_entry options and data."""
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default_value)
        )

    async def async_step_init(self, user_input=None):
        """Handle the initial step to choose location input method."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_LOCATION_METHOD, default=LOCATION_METHOD_LATLON): vol.In(
                    {
                        LOCATION_METHOD_LATLON: "Enter latitude/longitude",
                        LOCATION_METHOD_SEARCH: "Search for a location",
                    }
                ),
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=data_schema)

        if user_input[CONF_LOCATION_METHOD] == LOCATION_METHOD_LATLON:
            return await self.async_step_latlon()
        else:
            return await self.async_step_location_search()

    async def async_step_latlon(self, user_input=None):
        """Handle the latitude/longitude input step."""
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_LATITUDE,
                    default=self.get_default_value(CONF_LATITUDE, self.hass.config.latitude),
                ): float,
                vol.Required(
                    CONF_LONGITUDE,
                    default=self.get_default_value(CONF_LONGITUDE, self.hass.config.longitude),
                ): float,
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="latlon", data_schema=data_schema)

        errors, collector = await validate_location(self.hass, user_input)
        
        if errors:
            return self.async_show_form(
                step_id="latlon", data_schema=data_schema, errors=errors
            )
            
        # Store data and collector for future steps
        self.data.update(user_input) 
        self.collector = collector
        
        return await self.async_step_weather_name()

    async def async_step_location_search(self, user_input=None):
        """Handle the location search step."""
        data_schema = vol.Schema({
            vol.Required(CONF_LOCATION_SEARCH): str,
        })

        if user_input is None:
            return self.async_show_form(step_id="location_search", data_schema=data_schema)

        search_term = user_input[CONF_LOCATION_SEARCH]
        errors = {}
        locations = []

        try:
            # Use the collector's fetch_with_retry method for consistency
            import aiohttp
            headers = {"User-Agent": "MakeThisAPIOpenSource/1.0.0"}
            
            async with aiohttp.ClientSession(headers=headers) as session:
                search_url = f"{URL_BASE}{URL_LOCATION_SEARCH}{search_term}"
                data = await self.search_collector._fetch_with_retry(
                    session, search_url, "location_search"
                )
                
                if data and "data" in data and data["data"]:
                    locations = data["data"]
                else:
                    errors["base"] = "no_locations_found"
        except Exception:
            _LOGGER.exception("Unexpected exception during location search")
            errors["base"] = "unknown"
        
        if errors:
            return self.async_show_form(
                step_id="location_search", data_schema=data_schema, errors=errors
            )
        
        if not locations:
            errors["base"] = "no_locations_found"
            return self.async_show_form(
                step_id="location_search", data_schema=data_schema, errors=errors
            )
        
        # Store locations for selection step
        self.locations = locations
        return await self.async_step_location_selection()

    async def async_step_location_selection(self, user_input=None):
        """Handle location selection from search results."""
        # Create options with name, state and postcode
        options = {}
        for location in self.locations:
            geohash = location.get("geohash", "")
            name = location.get("name", "")
            state = location.get("state", "")
            postcode = location.get("postcode", "")
            display_name = f"{name}, {state} {postcode}"
            options[geohash] = display_name
        
        data_schema = vol.Schema({
            vol.Required(CONF_LOCATION_SELECTION): vol.In(options)
        })

        if user_input is None:
            return self.async_show_form(step_id="location_selection", data_schema=data_schema)

        # Get the selected location
        selected_geohash = user_input[CONF_LOCATION_SELECTION]
        selected_location = next(
            (loc for loc in self.locations if loc.get("geohash") == selected_geohash),
            None
        )
        
        if not selected_location:
            return self.async_abort(reason="location_not_found")
        
        # Extract lat/lon from the selected location
        latitude = selected_location.get("latitude")
        longitude = selected_location.get("longitude")
        
        if latitude is None or longitude is None:
            return self.async_abort(reason="invalid_location_data")
        
        # Create location data
        location_data = {
            CONF_LATITUDE: latitude,
            CONF_LONGITUDE: longitude,
        }
        
        # Validate the location
        errors, collector = await validate_location(self.hass, location_data)
        
        if errors:
            return self.async_show_form(
                step_id="location_selection", data_schema=data_schema, errors=errors
            )
        
        # Store data and collector for future steps
        self.data.update(location_data)
        self.collector = collector
        
        return await self.async_step_weather_name()

    async def async_step_weather_name(self, user_input=None):
        """Handle the locations step."""
        default_name = self.get_default_value(
            CONF_WEATHER_NAME, 
            self.collector.locations_data["data"]["name"]
        )
        
        data_schema = vol.Schema({
            vol.Required(CONF_WEATHER_NAME, default=default_name): str,
        })

        if user_input is None:
            return self.async_show_form(step_id="weather_name", data_schema=data_schema)

        errors = {}
        try:
            self.data.update(user_input)
            return await self.async_step_sensors_create()
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="weather_name", data_schema=data_schema, errors=errors
        )

    async def async_step_sensors_create(self, user_input=None):
        """Handle the observations step."""
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_OBSERVATIONS_CREATE,
                    default=self.get_default_value(CONF_OBSERVATIONS_CREATE, True),
                ): bool,
                vol.Required(
                    CONF_FORECASTS_CREATE,
                    default=self.get_default_value(CONF_FORECASTS_CREATE, True),
                ): bool,
                vol.Required(
                    CONF_WARNINGS_CREATE,
                    default=self.get_default_value(CONF_WARNINGS_CREATE, True),
                ): bool,
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="sensors_create", data_schema=data_schema)

        errors = {}
        try:
            self.data.update(user_input)
            
            # Determine next step based on selections
            if self.data[CONF_OBSERVATIONS_CREATE]:
                return await self.async_step_observations_monitored()
            elif self.data[CONF_FORECASTS_CREATE]:
                return await self.async_step_forecasts_monitored()
            elif self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            else:
                return self.async_create_entry(
                    title=self.collector.locations_data["data"]["name"],
                    data=self.data,
                )
                
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="sensors_create", data_schema=data_schema, errors=errors
        )

    async def async_step_observations_monitored(self, user_input=None):
        """Handle the observations monitored step."""
        default_basename = self.get_default_value(
            CONF_OBSERVATIONS_BASENAME,
            self.collector.observations_data["data"]["station"]["name"]
        )
        default_monitored = self.get_default_value(CONF_OBSERVATIONS_MONITORED, None)
        
        monitored = {sensor.key: sensor.name for sensor in OBSERVATION_SENSOR_TYPES}
        data_schema = vol.Schema(
            {
                vol.Required(CONF_OBSERVATIONS_BASENAME, default=default_basename): str,
                vol.Required(CONF_OBSERVATIONS_MONITORED, default=default_monitored): 
                    cv.multi_select(monitored),
            }
        )

        if user_input is None:
            return self.async_show_form(
                step_id="observations_monitored", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            
            # Determine next step based on selections
            if self.data[CONF_FORECASTS_CREATE]:
                return await self.async_step_forecasts_monitored()
            elif self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            else:
                return self.async_create_entry(
                    title=self.collector.locations_data["data"]["name"],
                    data=self.data,
                )
                
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="observations_monitored", data_schema=data_schema, errors=errors
        )

    async def async_step_forecasts_monitored(self, user_input=None):
        """Handle the forecasts monitored step."""
        default_basename = self.get_default_value(
            CONF_FORECASTS_BASENAME, 
            self.collector.locations_data["data"]["name"]
        )
        default_monitored = self.get_default_value(CONF_FORECASTS_MONITORED, None)
        default_days = self.get_default_value(CONF_FORECASTS_DAYS, 0)
        
        monitored = {sensor.key: sensor.name for sensor in FORECAST_SENSOR_TYPES}
        data_schema = vol.Schema(
            {
                vol.Required(CONF_FORECASTS_BASENAME, default=default_basename): str,
                vol.Required(CONF_FORECASTS_MONITORED, default=default_monitored): 
                    cv.multi_select(monitored),
                vol.Required(CONF_FORECASTS_DAYS, default=default_days): 
                    vol.All(vol.Coerce(int), vol.Range(0, 7)),
            }
        )

        if user_input is None:
            return self.async_show_form(
                step_id="forecasts_monitored", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            
            if self.data[CONF_WARNINGS_CREATE]:
                return await self.async_step_warnings_basename()
            
            return self.async_create_entry(
                title=self.collector.locations_data["data"]["name"],
                data=self.data
            )
            
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="forecasts_monitored", data_schema=data_schema, errors=errors
        )

    async def async_step_warnings_basename(self, user_input=None):
        """Handle the warnings basename step."""
        default_basename = self.get_default_value(
            CONF_WARNINGS_BASENAME, 
            self.collector.locations_data["data"]["name"]
        )
        
        data_schema = vol.Schema({
            vol.Required(CONF_WARNINGS_BASENAME, default=default_basename): str,
        })

        if user_input is None:
            return self.async_show_form(
                step_id="warnings_basename", data_schema=data_schema
            )

        errors = {}
        try:
            self.data.update(user_input)
            return self.async_create_entry(
                title=self.collector.locations_data["data"]["name"],
                data=self.data
            )
        except exceptions.CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="warnings_basename", data_schema=data_schema, errors=errors
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""