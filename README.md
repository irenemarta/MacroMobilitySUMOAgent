# Macro Mobility SUMO Agent

## Project objective
SUMO Digital Twin for macroscopic traffic assignment and what-if scenarios simulations through an AI agent able to translate italian written user requests into scenarios, comparing them to a baseline.
This work was born from a collaboration of Politecnico di Torino with CinqueT Srl and it has been set up as a case study over Turin Municiaplity. For several technical reasons, the selected area was the junction among Corso Francia, Corso Peschiera, Corso Telesio and Corso Brunelleschi, which is one of the main arteries connecting the city center to the outskirts. 

## Modules
The project runs on two main packages:
| Package | What it does |
|---|---|
| [`src/dtsumo`](src/dtsumo) | Digital twin pipeline, including data preparation and map parsing, iterative assignment ad-hoc algorithm leveraging both `marouter`and O'/D' extension with `duarouter`, mesoscopic SUMO simulation, validation against real CinqueT sensor data, execution and comparison of what-if scenarios. |
| [`src/agent`](src/agent) | Conversational agent (local LLM leveraging Qwen models via `llama.cpp`, OpenAI-compatible API) with a CLI and a web GUI (FastAPI + Leaflet). It creates scenarios in `dtsumo.whatif` format, has them validated and simulated, and shows the results on an interactive map. Detailed documentation (in Italian) in [`src/agent/ReadME.md`](src/agent/ReadME.md). |

## How it works


## Requirements and installation


## Usage