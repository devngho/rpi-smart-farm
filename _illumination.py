BH1750_ADDRESS = 0x23
CONTINUOUS_HIGH_RES_MODE = 0x10

try:
    import smbus2
    bus = smbus2.SMBus(1)
except Exception as e:
    bus = None
    print(f"Failed to initialize I2C bus: {e}")

def read_light():
    if bus is None:
        print("I2C bus not initialized, fallback")
        return -1.0
    
    data = bus.read_i2c_block_data(BH1750_ADDRESS, CONTINUOUS_HIGH_RES_MODE, 2)
    lux = (data[0] << 8) | data[1]
    return lux / 1.2