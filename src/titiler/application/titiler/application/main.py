"""titiler app."""

import json
import logging
from logging import config as log_config

import jinja2
import rasterio
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyQuery
from rio_tiler.io import Reader, STACReader
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from starlette_cramjam.middleware import CompressionMiddleware

from titiler.application import __version__ as titiler_version
from titiler.application.settings import ApiSettings
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import (
    AlgorithmFactory,
    ColorMapFactory,
    MultiBaseTilerFactory,
    TilerFactory,
    TMSFactory,
)
from titiler.core.middleware import (
    CacheControlMiddleware,
    LoggerMiddleware,
    LowerCaseQueryStringMiddleware,
    TotalTimeMiddleware,
)
from titiler.extensions import (
    cogValidateExtension,
    cogViewerExtension,
    stacExtension,
    stacRenderExtension,
    stacViewerExtension,
)
from titiler.mosaic.errors import MOSAIC_STATUS_CODES
from titiler.mosaic.factory import MosaicTilerFactory

logging.getLogger("botocore.credentials").disabled = True
logging.getLogger("botocore.utils").disabled = True
logging.getLogger("rio-tiler").setLevel(logging.ERROR)

jinja2_env = jinja2.Environment(
    loader=jinja2.ChoiceLoader([jinja2.PackageLoader(__package__, "templates")])
)
templates = Jinja2Templates(env=jinja2_env)


api_settings = ApiSettings()

app_dependencies = []
if api_settings.global_access_token:
    ###############################################################################
    # Setup a global API access key, if configured
    api_key_query = APIKeyQuery(name="access_token", auto_error=False)

    def validate_access_token(access_token: str = Security(api_key_query)):
        """Validates API key access token, set as the `api_settings.global_access_token` value.
        Returns True if no access token is required, or if the access token is valid.
        Raises an HTTPException (401) if the access token is required but invalid/missing.
        """
        if not access_token:
            raise HTTPException(status_code=401, detail="Missing `access_token`")

        # if access_token == `token` then OK
        if access_token != api_settings.global_access_token:
            raise HTTPException(status_code=401, detail="Invalid `access_token`")

        return True

    app_dependencies.append(Depends(validate_access_token))


###############################################################################

app = FastAPI(
    title=api_settings.name,
    openapi_url="/api",
    docs_url="/api.html",
    description="""A modern dynamic tile server built on top of FastAPI and Rasterio/GDAL.

---

**Documentation**: <a href="https://developmentseed.org/titiler/" target="_blank">https://developmentseed.org/titiler/</a>

**Source Code**: <a href="https://github.com/developmentseed/titiler" target="_blank">https://github.com/developmentseed/titiler</a>

---
    """,
    version=titiler_version,
    root_path=api_settings.root_path,
    dependencies=app_dependencies,
)

###############################################################################
# Simple Dataset endpoints (e.g Cloud Optimized GeoTIFF)
if not api_settings.disable_cog:
    cog = TilerFactory(
        reader=Reader,
        router_prefix="/cog",
        extensions=[
            cogValidateExtension(),
            cogViewerExtension(),
            stacExtension(),
        ],
    )

    app.include_router(
        cog.router,
        prefix="/cog",
        tags=["Cloud Optimized GeoTIFF"],
    )


###############################################################################
# STAC endpoints
if not api_settings.disable_stac:
    stac = MultiBaseTilerFactory(
        reader=STACReader,
        router_prefix="/stac",
        extensions=[
            stacViewerExtension(),
            stacRenderExtension(),
        ],
    )

    app.include_router(
        stac.router,
        prefix="/stac",
        tags=["SpatioTemporal Asset Catalog"],
    )

###############################################################################
# Mosaic endpoints
if not api_settings.disable_mosaic:
    mosaic = MosaicTilerFactory(router_prefix="/mosaicjson")
    app.include_router(
        mosaic.router,
        prefix="/mosaicjson",
        tags=["MosaicJSON"],
    )

###############################################################################
# TileMatrixSets endpoints
tms = TMSFactory()
app.include_router(
    tms.router,
    tags=["Tiling Schemes"],
)

###############################################################################
# Algorithms endpoints
algorithms = AlgorithmFactory()
app.include_router(
    algorithms.router,
    tags=["Algorithms"],
)

###############################################################################
# Colormaps endpoints
cmaps = ColorMapFactory()
app.include_router(
    cmaps.router,
    tags=["ColorMaps"],
)


add_exception_handlers(app, DEFAULT_STATUS_CODES)
add_exception_handlers(app, MOSAIC_STATUS_CODES)

# Set all CORS enabled origins
if api_settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_settings.cors_origins,
        allow_credentials=True,
        allow_methods=api_settings.cors_allow_methods,
        allow_headers=["*"],
    )

app.add_middleware(
    CompressionMiddleware,
    minimum_size=0,
    exclude_mediatype={
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/jp2",
        "image/webp",
    },
    compression_level=6,
)

app.add_middleware(
    CacheControlMiddleware,
    cachecontrol=api_settings.cachecontrol,
    exclude_path={r"/healthz"},
)

if api_settings.debug:
    app.add_middleware(LoggerMiddleware)
    app.add_middleware(TotalTimeMiddleware)

    log_config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "detailed": {
                    "format": "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
                },
                "request": {
                    "format": (
                        "%(asctime)s - %(levelname)s - %(name)s - %(message)s "
                        + json.dumps(
                            {
                                k: f"%({k})s"
                                for k in [
                                    "method",
                                    "referer",
                                    "origin",
                                    "route",
                                    "path",
                                    "path_params",
                                    "query_params",
                                    "headers",
                                ]
                            }
                        )
                    ),
                },
            },
            "handlers": {
                "console_detailed": {
                    "class": "logging.StreamHandler",
                    "level": "WARNING",
                    "formatter": "detailed",
                    "stream": "ext://sys.stdout",
                },
                "console_request": {
                    "class": "logging.StreamHandler",
                    "level": "DEBUG",
                    "formatter": "request",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "titlier": {
                    "level": "WARNING",
                    "handlers": ["console_detailed"],
                    "propagate": False,
                },
                "titiler-requests": {
                    "level": "INFO",
                    "handlers": ["console_request"],
                    "propagate": False,
                },
            },
        }
    )


if api_settings.lower_case_query_parameters:
    app.add_middleware(LowerCaseQueryStringMiddleware)


@app.get(
    "/healthz",
    description="Health Check.",
    summary="Health Check.",
    operation_id="healthCheck",
    tags=["Health Check"],
)
def application_health_check():
    """Health check."""
    return {
        "versions": {
            "titiler": titiler_version,
            "rasterio": rasterio.__version__,
            "gdal": rasterio.__gdal_version__,
            "proj": rasterio.__proj_version__,
            "geos": rasterio.__geos_version__,
        }
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing(request: Request):
    """TiTiler landing page."""
    urlpath = request.url.path
    if root_path := request.scope.get("root_path"):
        urlpath = urlpath.removeprefix(root_path)

    crumbs = []
    baseurl = str(request.base_url).rstrip("/")

    crumbpath = str(baseurl)
    if urlpath == "/":
        urlpath = ""

    for crumb in urlpath.split("/"):
        crumbpath = crumbpath.rstrip("/")
        part = crumb
        if part is None or part == "":
            part = "Home"
        crumbpath += f"/{crumb}"
        crumbs.append({"url": crumbpath.rstrip("/"), "part": part.capitalize()})

    data = {
        "title": "titiler",
        "links": [
            {
                "title": "Landing page",
                "href": str(request.url_for("landing")),
                "type": "text/html",
                "rel": "self",
            },
            {
                "title": "the API definition (JSON)",
                "href": str(request.url_for("openapi")),
                "type": "application/vnd.oai.openapi+json;version=3.0",
                "rel": "service-desc",
            },
            {
                "title": "the API documentation",
                "href": str(request.url_for("swagger_ui_html")),
                "type": "text/html",
                "rel": "service-doc",
            },
            {
                "title": "TiTiler Documentation (external link)",
                "href": "https://developmentseed.org/titiler/",
                "type": "text/html",
                "rel": "doc",
            },
            {
                "title": "TiTiler source code (external link)",
                "href": "https://github.com/developmentseed/titiler",
                "type": "text/html",
                "rel": "doc",
            },
        ],
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "response": data,
            "template": {
                "api_root": baseurl,
                "params": request.query_params,
                "title": "TiTiler",
            },
            "crumbs": crumbs,
        },
    )
