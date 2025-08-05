
import logging

POLLING_INTERVAL = 1.0 
SMOOTHING_TIME_S = 2.5

class HeaterPowerDistributor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        self.heater_list = config.getlist("heaters")
        self.max_total_power = config.getfloat("max_total_power", 1.0, above=0.0)

        self.managed_heaters =    {}
        self.original_max_power = {}
        self.smoothed_power =     {}

        self.pheaters = self.printer.load_object(config, 'heaters')
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_HEATER_GROUP_POWER", "GROUP", self.name,
                                    self.cmd_SET_HEATER_GROUP_POWER,
                                    desc=self.cmd_SET_HEATER_GROUP_POWER_help)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        for name in self.heater_list:
            heater = self.pheaters.lookup_heater(name)
            self.managed_heaters[name] = heater
            self.original_max_power[name] = heater.get_max_power()
            self.smoothed_power[name] = 0.0
        
        if not self.managed_heaters:
           raise self.printer.config_error("this module requires heaters.")

        reactor = self.printer.get_reactor()
        self.timer = reactor.register_timer(self._update_callback, 
                                            reactor.monotonic() + POLLING_INTERVAL)
        
        logging.info(f"HeaterPowerDistributor '{self.name}' managing heaters: "
                        f"{list(self.managed_heaters.keys())}")

    cmd_SET_HEATER_GROUP_POWER_help = "Sets max total power for a reactive heater group"
    def cmd_SET_HEATER_GROUP_POWER(self, gcmd):
        new_power = gcmd.get_float('POWER', above=0.0)
        self.max_total_power = new_power
        gcmd.respond_info(f"HeaterPowerDistributor '{self.name}' \
                            max_total_power set to "f"{self.max_total_power*100}%.")

    def _update_callback(self, eventtime):
        total_smoothed_power = 0.0
        # (EMA) == Exponential Moving Average 
        for name, heater in self.managed_heaters.items():
            current_power = heater.get_status(eventtime)['power']
            current_ema = self.smoothed_power[name]
            new_ema = (current_power * (POLLING_INTERVAL / SMOOTHING_TIME_S)) \
                       + (current_ema * (1 - self.alpha))
            self.smoothed_power[name] = new_ema
            total_smoothed_power += new_ema

        if total_smoothed_power > self.max_total_power:
            scaling_factor = self.max_total_power / total_smoothed_power
            for name, heater in self.managed_heaters.items():
                new_max_power = self.smoothed_power[name] * scaling_factor
                heater.control.heater_max_power = min(self.original_max_power[name], new_max_power)
        else:
            for name, heater in self.managed_heaters.items():
                heater.control.heater_max_power = self.original_max_power[name]

        return eventtime + POLLING_INTERVAL

def load_config_prefix(config):
    return HeaterPowerDistributor(config)