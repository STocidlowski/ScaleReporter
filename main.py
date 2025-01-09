#!/usr/bin/env python3

"""This interfaces to an electronic scale via USB, and reports the weight. Serves the data on port 8080.

Default setting is for a "Health o meter" scale. Tested using 1100L which uses "CP210x USB to UART Bridge" serial.
If this is run from windows, you will have to install this driver.

local testing webpage: http://localhost:8080/
"""

import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from time import sleep
import threading

import serial
from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo
from pydantic import BaseModel

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.responses import FileResponse
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# FastAPI app
app = FastAPI()

class WeightUOMVS(Enum):
    """Weight Units of Measure - based on "Unified Code for Units of Measure" - http://unitsofmeasure.org
    Describe the units of measure associated with the measured weight value.
    http://hl7.org/fhir/us/vitals/ValueSet/WeightUOMVS
    """
    GRAM = "g"
    KILOGRAM = "kg"
    POUND = "[lb_av]" # pound in the "avoirdupois weight" system - The US pound


class ScaleWeight(BaseModel):
    """Represents the parsed weight data from the scale."""
    event_time: datetime
    patient_id: Optional[str]
    weight: float
    height: Optional[float]
    bmi: Optional[float]
    units: WeightUOMVS

# Global variables
last_weight: Optional[ScaleWeight] = None
connected_clients: list[WebSocket] = []  # List of connected WebSocket clients


def parse_packet(packet: str) -> Optional[ScaleWeight]:
    """Parses the data packet and extracts relevant information.

    Args:
        packet (str): Raw data packet from the scale.

    Returns:
        ScaleWeight: Parsed weight data object, or None on failure.

    >>> parse_packet("6RI0000000000W123.4H0.0B0.0T0.0NcE")
    123.4 kg
    """
    global last_weight
    try:
        # Parsing logic remains the same
        fields = packet.split("\x1B")
        patient_id = next((field[1:] for field in fields if field.startswith("I")), None)
        weight = float(next((field[1:] for field in fields if field.startswith("W")), "0"))
        height = float(next((field[1:] for field in fields if field.startswith("H")), "0"))
        bmi = float(next((field[1:] for field in fields if field.startswith("B")), "0"))
        units_code = next((field[1:] for field in fields if field.startswith("N")), "c")
        units = WeightUOMVS.KILOGRAM if units_code == "m" else WeightUOMVS.POUND

        last_weight = ScaleWeight(
            event_time=datetime.now(),
            patient_id=patient_id,
            weight=weight,
            height=height,
            bmi=bmi,
            units=units,
        )
        logging.info(f"Parsed Data: {last_weight}")

        # Notify WebSocket clients
        notify_clients(last_weight.model_dump_json())
        return last_weight

    except Exception as e:
        logging.error(f"Error parsing packet: {e}")
        return None


def find_scale_port(description_text: str = "USB to UART Bridge") -> Optional[str]:
    """Finds the serial port corresponding to the scale device.

    Args:
        description_text (str): Partial description to identify the scale.

    Returns:
        str | None: The port name if found, or None otherwise.

    Note - The same controller reports differently based on OS:
        Windows: COM3 (Silicon Labs CP210x USB to UART Bridge (COM3))
        Linux: /dev/ttyUSB0 (CP2102 USB to UART Bridge Controller - CP2102 USB to UART Bridge Controller)
    """
    ports: list[ListPortInfo] = comports()
    for port in ports:
        if description_text in port.description:
            logging.info(f"Found scale - {port.device} ({port.description})")
            return port.device
    return None


def list_serial_ports():
    """Lists all available serial ports on the system."""
    ports: list[ListPortInfo] = comports()
    if not ports:
        logging.info("No serial ports found.")
        return

    logging.info("Available serial ports:")
    for port in ports:
        logging.info(f"- {port.device} ({port.description})")


def main(port: str, baud_rate: int = 9600):
    """Reads data from the USB serial port and processes it.

    Args:
        port (str): The serial port to connect to.
        baud_rate (int): The baud rate for the serial connection.
    """

    try:
        with serial.Serial(port, baud_rate, timeout=1) as ser:
            logging.info(f"Listening on {port} at {baud_rate} baud...")
            while True:
                line = ser.readline().decode("utf-8").strip()
                if line:
                    logging.info(f"Received Packet: {line}")
                    weight_data = parse_packet(packet=line)
                    if weight_data:
                        logging.info(f"Weight Data: {weight_data.model_dump_json()}")
    except serial.SerialException as e:
        logging.error(f"Serial error: {e}")
    except KeyboardInterrupt:
        logging.info("Exiting on user interrupt.")


def notify_clients(data: str):
    """Notify all connected WebSocket clients with new data."""
    global connected_clients
    to_remove = []

    for client in connected_clients:
        try:
            import asyncio
            asyncio.run(client.send_text(data))
        except Exception as e:
            logging.error(f"Error sending data to WebSocket client: {e}")
            to_remove.append(client)

    # Remove disconnected clients
    for client in to_remove:
        connected_clients.remove(client)


@app.get("/", response_class=HTMLResponse)
def get_webpage():
    """HTML endpoint, serves a page with javascript for real-time weight updates."""
    # TODO: API key authorization, or user authentication. TLS / encryption would be necessary before prod

    base_dir = Path(__file__).resolve().parent
    # Construct the full path to the index.html file
    html_path = base_dir / "html" / "static" / "index.html"

    return FileResponse(html_path)


@app.get("/api", response_model=Optional[ScaleWeight])
def get_api():
    """Serve the latest weight data as JSON."""
    global last_weight
    return last_weight


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time weight updates."""
    global last_weight
    await websocket.accept()
    connected_clients.append(websocket)
    logging.info("WebSocket client connected.")

    if last_weight:
        await websocket.send_text(last_weight.model_dump_json())

    try:
        while True:
            await websocket.receive_text()  # Keep the connection open
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        logging.info("WebSocket client disconnected.")


if __name__ == "__main__":
    # Start the serial reading in a separate thread
    def serial_thread():
        list_serial_ports()
        while True:
            scale_port = find_scale_port()
            if scale_port:
                main(port=scale_port)
            sleep(5)

    threading.Thread(target=serial_thread, daemon=True).start()

    logging.info("""
    Webpage: http://localhost:8080/
    API: http://localhost:8080/api
    WebSocket: ws://localhost:8080/ws
    """)

    uvicorn.run(app, host="0.0.0.0", port=8080)
