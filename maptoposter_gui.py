import customtkinter as ctk
import subprocess
import sys
import threading
import logging
import os
import json
import re
from pathlib import Path
import matplotlib.font_manager as fm
from tkinter import messagebox

logger = logging.getLogger("maptoposter")

# Standard Appearance Settings
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class ModernMapPosterGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        # Window Configuration
        self.title("Map Studio")
        self.geometry("1400x900")
        self.minsize(1200, 800)

        # Grid Configuration
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=2)
        self.grid_rowconfigure(1, weight=1)

        # Path and Data Setup
        if getattr(sys, 'frozen', False):
            self.base_path = Path(sys.executable).parent
        else:
            self.base_path = Path(__file__).parent

        self.script_path = self.base_path / "create_map_poster.py"
        self.themes_dir = self.base_path / "themes"
        self.settings_file = self.base_path / "gui_settings.json"

        # Load settings
        self.settings = self._load_settings()

        # Load themes and fonts (cached)
        self.themes = self._sync_themes()
        self.theme_descriptions = self._load_theme_descriptions()
        self._installed_fonts: list[str] | None = None

        # Generation state
        self._generating: bool = False
        self._process: subprocess.Popen | None = None

        # Original stdio (saved before any redirection in frozen mode)
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        # Resolution Presets
        self.resolutions: dict[str, tuple[float, float]] = {
            "Poster (12x16)": (12, 16),
            "A4 Print (8.3x11.7)": (8.3, 11.7),
            "4K Wallpaper (12.8x7.2)": (12.8, 7.2),
            "HD Wallpaper (6.4x3.6)": (6.4, 3.6),
            "Mobile Portrait (3.6x6.4)": (3.6, 6.4),
            "Instagram Square (3.6x3.6)": (3.6, 3.6),
        }

        self.setup_ui()
        self.check_script_exists()
        self._load_last_used_settings()

        # Keyboard shortcuts
        self.bind("<Control-g>", lambda e: self.start_generation())
        self.bind("<Control-o>", lambda e: self.open_output_folder())
        self.bind("<Control-s>", lambda e: self.save_preset_dialog())

        # Window close handler
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    @property
    def installed_fonts(self) -> list[str]:
        """Lazily load and cache installed fonts."""
        if self._installed_fonts is None:
            self._installed_fonts = self._get_installed_fonts()
        return self._installed_fonts

    def _load_settings(self) -> dict:
        """Load GUI settings from file."""
        default_settings: dict = {
            "recent_locations": [],
            "favorite_themes": [],
            "presets": {},
            "last_used": {}
        }
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r') as f:
                    loaded = json.load(f)
            except (json.JSONDecodeError, OSError):
                return default_settings
            # Migrate legacy recent_cities (list of "City, Country" strings)
            # to recent_locations (list of full snapshot dicts).
            if "recent_cities" in loaded and "recent_locations" not in loaded:
                loaded["recent_locations"] = [
                    self._recent_entry_from_legacy_string(s)
                    for s in loaded.pop("recent_cities")
                    if isinstance(s, str) and ", " in s
                ]
            loaded.setdefault("recent_locations", [])
            return loaded
        return default_settings

    @staticmethod
    def _recent_entry_from_legacy_string(s: str) -> dict:
        """Convert a legacy 'City, Country' string into a full Recent entry."""
        city, _, country = s.partition(", ")
        return {
            "mode": "city",
            "city": city, "country": country,
            "latitude": "", "longitude": "",
            "city_override": "", "country_override": "",
        }

    def _save_settings(self) -> None:
        """Save GUI settings to file."""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            logger.warning("Could not save settings: %s", e)

    def _apply_settings_to_form(self, data: dict) -> None:
        """Apply a settings dictionary (preset or last_used) to the GUI form."""
        try:
            theme = data.get("theme", "feature_based")
            self.theme_menu.set(theme)
            self._on_theme_change(theme)
            self.font_menu.set(data.get("font", "Arial"))
            self.city_font_size_var.set(data.get("city_font_size", "60"))
            self.country_font_size_var.set(data.get("country_font_size", "30"))
            self.coords_font_size_var.set(data.get("coords_font_size", "22"))

            if data.get("custom_text_sizes", False):
                self.custom_text_size_sw.select()
            else:
                self.custom_text_size_sw.deselect()
            self.toggle_custom_text_sizes()

            self.dist_entry.delete(0, "end")
            self.dist_entry.insert(0, data.get("distance", "15000"))
            self.network_menu.set(data.get("network", "drive"))
            self.w_var.set(data.get("width", "12"))
            self.h_var.set(data.get("height", "16"))
            self.dpi_entry.delete(0, "end")
            self.dpi_entry.insert(0, data.get("dpi", "300"))

            format_val = data.get("format", "png")
            self.format_menu.set(format_val)
            self.on_format_change(format_val)

            if data.get("svg_layers", False):
                self.svg_layers_sw.select()
            else:
                self.svg_layers_sw.deselect()

            self.stl_base_var.set(data.get("stl_base", "3.0"))
            self.stl_height_var.set(data.get("stl_height", "2.5"))
            self.stl_res_var.set(data.get("stl_resolution", "800"))
            self.stl_smooth_var.set(data.get("stl_smoothing", "1.0"))

            if data.get("stl_invert", False):
                self.stl_invert_sw.select()
            else:
                self.stl_invert_sw.deselect()

            for sw, key, default in [
                (self.text_sw, "text", True),
                (self.road_sw, "roads", True),
                (self.water_sw, "water", True),
                (self.park_sw, "parks", True),
                (self.sea_sw, "show_sea", False),
                (self.wetlands_sw, "show_wetlands", False),
                (self.religious_sw, "show_religious", False),
                (self.historic_sw, "show_historic", False),
            ]:
                if data.get(key, default):
                    sw.select()
                else:
                    sw.deselect()
        except Exception as e:
            logger.debug("Error applying settings to form: %s", e)

    def _collect_form_settings(self) -> dict:
        """Collect current form values into a settings dict."""
        return {
            "theme": self.theme_menu.get(),
            "font": self.font_menu.get(),
            "city_font_size": self.city_font_size_var.get(),
            "country_font_size": self.country_font_size_var.get(),
            "coords_font_size": self.coords_font_size_var.get(),
            "custom_text_sizes": self.custom_text_size_sw.get(),
            "distance": self.dist_entry.get(),
            "network": self.network_menu.get(),
            "width": self.w_var.get(),
            "height": self.h_var.get(),
            "dpi": self.dpi_entry.get(),
            "format": self.format_menu.get(),
            "svg_layers": self.svg_layers_sw.get(),
            "stl_base": self.stl_base_var.get(),
            "stl_height": self.stl_height_var.get(),
            "stl_resolution": self.stl_res_var.get(),
            "stl_smoothing": self.stl_smooth_var.get(),
            "stl_invert": self.stl_invert_sw.get(),
            "text": self.text_sw.get(),
            "roads": self.road_sw.get(),
            "water": self.water_sw.get(),
            "parks": self.park_sw.get(),
            "show_sea": self.sea_sw.get(),
            "show_wetlands": self.wetlands_sw.get(),
            "show_religious": self.religious_sw.get(),
            "show_historic": self.historic_sw.get(),
        }

    def _collect_recent_entry(self) -> dict:
        """Snapshot the current location inputs for the Recent dropdown."""
        return {
            "mode": self.location_mode.get(),
            "city": self.city_entry.get().strip(),
            "country": self.country_entry.get().strip(),
            "latitude": self.lat_entry.get().strip(),
            "longitude": self.lon_entry.get().strip(),
            "city_override": self.city_override_entry.get().strip(),
            "country_override": self.country_override_entry.get().strip(),
        }

    @staticmethod
    def _recent_label(entry: dict) -> str:
        """Human-readable label for a Recent entry."""
        if entry.get("mode") == "coords":
            coords = f"{entry.get('latitude', '')}, {entry.get('longitude', '')}"
            co_city = entry.get("city_override", "")
            co_country = entry.get("country_override", "")
            if co_city and co_country:
                return f"{co_city}, {co_country} ({coords})"
            return coords
        return f"{entry.get('city', '')}, {entry.get('country', '')}"

    def add_recent_location(self, entry: dict) -> None:
        """Add a full location snapshot to the Recent dropdown."""
        locations = self.settings["recent_locations"]
        if entry in locations:
            locations.remove(entry)
        locations.insert(0, entry)
        self.settings["recent_locations"] = locations[:5]
        self._save_settings()
        self.update_recent_locations_menu()

    def update_recent_locations_menu(self) -> None:
        """Update the Recent dropdown values from saved entries."""
        if hasattr(self, 'recent_menu'):
            entries = self.settings["recent_locations"]
            labels = [self._recent_label(e) for e in entries] if entries else ["No recent locations"]
            self.recent_menu.configure(values=labels)
            if labels[0] != "No recent locations":
                self.recent_menu.set(labels[0])

    def load_recent_location(self, label: str) -> None:
        """Restore mode and all location fields from a Recent entry."""
        if not label or label == "No recent locations":
            return
        for entry in self.settings["recent_locations"]:
            if self._recent_label(entry) == label:
                self._apply_recent_entry(entry)
                return

    def _apply_recent_entry(self, entry: dict) -> None:
        """Apply a Recent entry to the form (mode + all six location fields)."""
        self.location_mode.set(entry.get("mode", "city"))
        self.toggle_location_mode()
        for widget, key in [
            (self.city_entry, "city"),
            (self.country_entry, "country"),
            (self.lat_entry, "latitude"),
            (self.lon_entry, "longitude"),
            (self.city_override_entry, "city_override"),
            (self.country_override_entry, "country_override"),
        ]:
            widget.delete(0, "end")
            widget.insert(0, entry.get(key, ""))

    def toggle_favorite_theme(self) -> None:
        """Add/remove current theme from favorites."""
        current_theme = self.theme_menu.get()
        if current_theme in self.settings["favorite_themes"]:
            self.settings["favorite_themes"].remove(current_theme)
            self.fav_btn.configure(text="* Favorite")
            messagebox.showinfo("Removed", f"Removed {current_theme} from favorites")
        else:
            self.settings["favorite_themes"].append(current_theme)
            self.fav_btn.configure(text="* Favorited")
            messagebox.showinfo("Added", f"Added {current_theme} to favorites")
        self._save_settings()
        self.update_theme_menu()

    def update_theme_menu(self) -> None:
        """Update theme menu with favorites at top."""
        favorites = [t for t in self.settings["favorite_themes"] if t in self.themes]
        others = [t for t in self.themes if t not in favorites]
        if favorites:
            theme_list = favorites + ["---"] + others
        else:
            theme_list = others
        self.theme_menu.configure(values=theme_list)

    def save_last_used(self) -> None:
        """Save current settings as last used."""
        self.settings["last_used"] = self._collect_form_settings()
        self._save_settings()

    def _load_last_used_settings(self) -> None:
        """Load last used settings into form."""
        if self.settings.get("last_used"):
            self._apply_settings_to_form(self.settings["last_used"])

    def save_preset_dialog(self) -> None:
        """Show dialog to save current settings as preset."""
        dialog = ctk.CTkInputDialog(text="Enter preset name:", title="Save Preset")
        preset_name = dialog.get_input()
        if preset_name:
            preset_data = self._collect_form_settings()
            preset_data["city"] = self.city_entry.get()
            preset_data["country"] = self.country_entry.get()
            self.settings["presets"][preset_name] = preset_data
            self._save_settings()
            self.update_presets_menu()
            messagebox.showinfo("Saved", f"Preset '{preset_name}' saved!")

    def load_preset(self, preset_name: str) -> None:
        """Load a saved preset."""
        if preset_name in self.settings["presets"]:
            preset = self.settings["presets"][preset_name]
            self.city_entry.delete(0, "end")
            self.city_entry.insert(0, preset.get("city", ""))
            self.country_entry.delete(0, "end")
            self.country_entry.insert(0, preset.get("country", ""))
            self._apply_settings_to_form(preset)

    def update_presets_menu(self) -> None:
        """Update the presets dropdown."""
        if hasattr(self, 'preset_menu'):
            preset_list = list(self.settings["presets"].keys()) if self.settings["presets"] else ["No presets"]
            self.preset_menu.configure(values=preset_list)

    def _get_installed_fonts(self) -> list[str]:
        """Get sorted list of unique installed fonts (called once, cached)."""
        try:
            fonts = sorted(set(f.name for f in fm.fontManager.ttflist))
            priority = ["Arial", "Helvetica", "Times New Roman", "Roboto"]
            for font in reversed(priority):
                if font in fonts:
                    fonts.remove(font)
                    fonts.insert(0, font)
            return fonts
        except Exception as e:
            logger.warning("Error loading installed fonts: %s", e)
            return ["Arial", "Helvetica", "Times New Roman"]

    def _sync_themes(self) -> list[str]:
        """Scan themes directory for all JSON theme files."""
        themes: list[str] = []
        if self.themes_dir.exists():
            try:
                themes = sorted(f.stem for f in self.themes_dir.glob("*.json"))
            except Exception as e:
                logger.warning("Error scanning themes directory: %s", e)

        if not themes:
            themes = [
                "feature_based", "gradient_roads", "contrast_zones", "noir",
                "midnight_blue", "blueprint", "neon_cyberpunk", "warm_beige",
                "pastel_dream", "japanese_ink", "forest", "ocean",
                "terracotta", "sunset", "autumn", "copper_patina", "monochrome_blue"
            ]
        return themes

    def _load_theme_descriptions(self) -> dict[str, str]:
        """Load description field from each theme JSON."""
        descriptions: dict[str, str] = {}
        if not self.themes_dir.exists():
            return descriptions
        for path in self.themes_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                desc = data.get("description", "")
                if desc:
                    descriptions[path.stem] = desc
            except Exception:
                pass
        return descriptions

    def _on_theme_change(self, theme: str) -> None:
        """Update description label when theme selection changes."""
        desc = self.theme_descriptions.get(theme, "")
        self.theme_desc_label.configure(text=desc)

    def check_script_exists(self) -> None:
        """Verify the main script exists (only relevant in script/dev mode).

        When running as a frozen EXE, create_map_poster is a bundled module
        and no .py file is needed — skip the check entirely.
        """
        if getattr(sys, 'frozen', False):
            return  # Bundled EXE uses direct import; no .py file required
        if not self.script_path.exists():
            messagebox.showwarning(
                "Script Not Found",
                f"Cannot find {self.script_path}\n\nPlease ensure create_map_poster.py is in the same directory."
            )

    def setup_ui(self) -> None:
        # ===== TOP SECTION: CONTROLS IN COLUMNS =====
        controls_container = ctk.CTkFrame(self, fg_color="gray17")
        controls_container.grid(row=0, column=0, sticky="nsew", padx=20, pady=(20, 10))

        controls_container.grid_rowconfigure(1, weight=1)
        for i in range(4):
            controls_container.grid_columnconfigure(i, weight=1, uniform="column")

        # Brand Header
        brand = ctk.CTkLabel(
            controls_container,
            text="MAP STUDIO",
            font=ctk.CTkFont(size=24, weight="bold")
        )
        brand.grid(row=0, column=0, columnspan=4, pady=(15, 20))

        # ========== COLUMN 1: LOCATION ==========
        col1 = ctk.CTkFrame(controls_container, fg_color="gray20")
        col1.grid(row=1, column=0, sticky="nsew", padx=10, pady=10, ipady=10)

        self.add_column_header(col1, "LOCATION")

        ctk.CTkLabel(col1, text="Recent:", anchor="w", font=ctk.CTkFont(size=10),
                    text_color="gray50").pack(fill="x", padx=15, pady=(0, 5))
        self.recent_menu = ctk.CTkOptionMenu(
            col1, values=["No recent locations"], command=self.load_recent_location
        )
        self.recent_menu.pack(fill="x", padx=15, pady=(0, 12))
        self.update_recent_locations_menu()

        self.location_mode = ctk.StringVar(value="city")
        mode_frame = ctk.CTkFrame(col1, fg_color="gray15")
        mode_frame.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkRadioButton(
            mode_frame, text=" City",
            variable=self.location_mode, value="city",
            command=self.toggle_location_mode
        ).pack(anchor="w", padx=10, pady=5)

        ctk.CTkRadioButton(
            mode_frame, text=" Coordinates",
            variable=self.location_mode, value="coords",
            command=self.toggle_location_mode
        ).pack(anchor="w", padx=10, pady=5)

        self.input_container = ctk.CTkFrame(col1, fg_color="transparent")
        self.input_container.pack(fill="x", padx=15, pady=(0, 10))

        self.city_frame = ctk.CTkFrame(self.input_container, fg_color="transparent")
        self.city_frame.pack(fill="x")

        self.city_entry = ctk.CTkEntry(self.city_frame, placeholder_text="City", height=32)
        self.city_entry.pack(fill="x", pady=(0, 8))

        self.country_entry = ctk.CTkEntry(self.city_frame, placeholder_text="Country", height=32)
        self.country_entry.pack(fill="x", pady=(0, 8))

        self.coord_frame = ctk.CTkFrame(self.input_container, fg_color="transparent")

        self.lat_entry = ctk.CTkEntry(self.coord_frame, placeholder_text="Latitude", height=32)
        self.lat_entry.pack(fill="x", pady=(0, 8))

        self.lon_entry = ctk.CTkEntry(self.coord_frame, placeholder_text="Longitude", height=32)
        self.lon_entry.pack(fill="x", pady=(0, 8))

        self.coord_frame.pack_forget()

        ctk.CTkLabel(col1, text="Custom Display:",
                    font=ctk.CTkFont(size=10), text_color="gray50").pack(padx=15, anchor="w")
        self.city_override_entry = ctk.CTkEntry(col1, placeholder_text="Custom city", height=32)
        self.city_override_entry.pack(fill="x", padx=15, pady=(5, 8))

        self.country_override_entry = ctk.CTkEntry(col1, placeholder_text="Custom country", height=32)
        self.country_override_entry.pack(fill="x", padx=15, pady=(0, 15))

        # ========== COLUMN 2: MAP SETTINGS ==========
        col2 = ctk.CTkFrame(controls_container, fg_color="gray20")
        col2.grid(row=1, column=1, sticky="nsew", padx=10, pady=10, ipady=10)

        self.add_column_header(col2, "MAP DESIGN")

        theme_frame = ctk.CTkFrame(col2, fg_color="transparent")
        theme_frame.pack(fill="x", padx=15, pady=(0, 5))

        ctk.CTkLabel(theme_frame, text="Theme:", anchor="w").pack(side="left")
        self.fav_btn = ctk.CTkButton(
            theme_frame, text="*", width=30, height=24,
            command=self.toggle_favorite_theme
        )
        self.fav_btn.pack(side="right")

        self.theme_menu = ctk.CTkOptionMenu(col2, values=self.themes, width=200,
                                             command=self._on_theme_change)
        self.theme_menu.pack(fill="x", padx=15, pady=(0, 4))
        if self.themes:
            self.theme_menu.set(self.themes[0])
        self.update_theme_menu()

        self.theme_desc_label = ctk.CTkLabel(
            col2, text="", anchor="w", wraplength=200,
            font=ctk.CTkFont(size=11), text_color="gray60"
        )
        self.theme_desc_label.pack(fill="x", padx=15, pady=(0, 8))
        self._on_theme_change(self.theme_menu.get())

        ctk.CTkLabel(col2, text="Font:", anchor="w").pack(fill="x", padx=15, pady=(0, 5))
        self.font_menu = ctk.CTkComboBox(col2, values=self.installed_fonts, width=200)
        self.font_menu.set("Arial")
        self.font_menu.pack(fill="x", padx=15, pady=(0, 12))

        # Text Size Controls
        text_size_frame = ctk.CTkFrame(col2, fg_color="gray15", corner_radius=6)
        text_size_frame.pack(fill="x", padx=15, pady=(0, 12))

        text_size_header = ctk.CTkFrame(text_size_frame, fg_color="transparent")
        text_size_header.pack(fill="x", padx=10, pady=(8, 5))

        ctk.CTkLabel(text_size_header, text="Text Sizes:", anchor="w",
                    font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")

        self.custom_text_size_sw = ctk.CTkSwitch(
            text_size_header, text="Custom", font=ctk.CTkFont(size=10),
            width=36, height=18, command=self.toggle_custom_text_sizes
        )
        self.custom_text_size_sw.pack(side="right")

        # Dynamic theme/font widgets created here and stored as self.<var_name> for later access
        # Font size entries
        for label, var_name, default in [
            ("City:", "city_font_size_var", "60"),
            ("Country:", "country_font_size_var", "30"),
            ("Coords:", "coords_font_size_var", "22"),
        ]:
            row = ctk.CTkFrame(text_size_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(row, text=label, width=55, anchor="w",
                        font=ctk.CTkFont(size=10)).pack(side="left")
            var = ctk.StringVar(value=default)
            setattr(self, var_name, var)
            entry = ctk.CTkEntry(row, textvariable=var, width=50, height=24, state="disabled")
            entry.pack(side="left", padx=2)
            setattr(self, var_name.replace("_var", "_entry"), entry)
            ctk.CTkLabel(row, text="pt", text_color="gray50",
                        font=ctk.CTkFont(size=9)).pack(side="left")

        ctk.CTkLabel(col2, text="Radius (m):", anchor="w").pack(fill="x", padx=15, pady=(0, 5))
        self.dist_entry = ctk.CTkEntry(col2, height=32)
        self.dist_entry.insert(0, "15000")
        self.dist_entry.pack(fill="x", padx=15, pady=(0, 5))
        ctk.CTkLabel(col2, text="4k-6k: Small | 8k-12k: Med | 15k+: Large",
                    font=ctk.CTkFont(size=9), text_color="gray50").pack(padx=15, pady=(0, 12))

        ctk.CTkLabel(col2, text="Network:", anchor="w").pack(fill="x", padx=15, pady=(0, 5))
        self.network_menu = ctk.CTkOptionMenu(col2, values=["drive", "all", "walk", "bike"])
        self.network_menu.set("drive")
        self.network_menu.pack(fill="x", padx=15, pady=(0, 15))

        # ========== COLUMN 3: OUTPUT SETTINGS ==========
        col3 = ctk.CTkFrame(controls_container, fg_color="gray20")
        col3.grid(row=1, column=2, sticky="nsew", padx=10, pady=10, ipady=10)

        self.add_column_header(col3, "OUTPUT")

        ctk.CTkLabel(col3, text="Format:", anchor="w").pack(fill="x", padx=15, pady=(0, 5))
        self.format_menu = ctk.CTkOptionMenu(col3, values=["png", "svg", "pdf", "stl"],
                                             command=self.on_format_change)
        self.format_menu.set("png")
        self.format_menu.pack(fill="x", padx=15, pady=(0, 10))

        self.format_settings_container = ctk.CTkFrame(col3, fg_color="transparent")
        self.format_settings_container.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        # PNG/PDF SETTINGS
        self.png_settings_frame = ctk.CTkFrame(self.format_settings_container, fg_color="transparent")

        ctk.CTkLabel(self.png_settings_frame, text="Size Preset:", anchor="w").pack(fill="x", pady=(0, 5))
        self.size_preset = ctk.CTkOptionMenu(
            self.png_settings_frame, values=list(self.resolutions.keys()),
            command=self.set_preset
        )
        self.size_preset.pack(fill="x", pady=(0, 5))

        dim_frame = ctk.CTkFrame(self.png_settings_frame, fg_color="transparent")
        dim_frame.pack(fill="x", pady=(0, 10))

        self.w_var = ctk.StringVar(value="12")
        self.h_var = ctk.StringVar(value="16")

        ctk.CTkLabel(dim_frame, text="W:", width=25).pack(side="left")
        ctk.CTkEntry(dim_frame, textvariable=self.w_var, width=60, height=32).pack(side="left", padx=(5, 10))
        ctk.CTkLabel(dim_frame, text="H:", width=25).pack(side="left")
        ctk.CTkEntry(dim_frame, textvariable=self.h_var, width=60, height=32).pack(side="left", padx=(5, 10))
        ctk.CTkLabel(dim_frame, text="in", text_color="gray50").pack(side="left")

        ctk.CTkLabel(self.png_settings_frame, text="DPI:", anchor="w").pack(fill="x", pady=(0, 5))
        self.dpi_entry = ctk.CTkEntry(self.png_settings_frame, height=32)
        self.dpi_entry.insert(0, "300")
        self.dpi_entry.pack(fill="x", pady=(0, 3))
        ctk.CTkLabel(self.png_settings_frame, text="72:Screen | 300:Print",
                    font=ctk.CTkFont(size=9), text_color="gray50").pack(pady=(0, 5))

        # SVG SETTINGS
        self.svg_settings_frame = ctk.CTkFrame(self.format_settings_container, fg_color="transparent")

        ctk.CTkLabel(self.svg_settings_frame, text="Size Preset:", anchor="w").pack(fill="x", pady=(0, 5))
        self.svg_size_preset = ctk.CTkOptionMenu(
            self.svg_settings_frame, values=list(self.resolutions.keys()),
            command=self.set_preset
        )
        self.svg_size_preset.pack(fill="x", pady=(0, 5))

        svg_dim_frame = ctk.CTkFrame(self.svg_settings_frame, fg_color="transparent")
        svg_dim_frame.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(svg_dim_frame, text="W:", width=25).pack(side="left")
        ctk.CTkEntry(svg_dim_frame, textvariable=self.w_var, width=60, height=32).pack(side="left", padx=(5, 10))
        ctk.CTkLabel(svg_dim_frame, text="H:", width=25).pack(side="left")
        ctk.CTkEntry(svg_dim_frame, textvariable=self.h_var, width=60, height=32).pack(side="left", padx=(5, 10))
        ctk.CTkLabel(svg_dim_frame, text="in", text_color="gray50").pack(side="left")

        svg_layers_container = ctk.CTkFrame(self.svg_settings_frame, fg_color="gray15", corner_radius=6)
        svg_layers_container.pack(fill="x", pady=(0, 5))

        self.svg_layers_sw = ctk.CTkSwitch(svg_layers_container, text=" SVG Layers (plotter-friendly)",
                                          font=ctk.CTkFont(size=10))
        self.svg_layers_sw.pack(anchor="w", padx=10, pady=8)
        ctk.CTkLabel(svg_layers_container, text="Organize roads by width",
                    font=ctk.CTkFont(size=9), text_color="gray50").pack(padx=10, pady=(0, 5))

        # STL SETTINGS
        self.stl_settings_frame = ctk.CTkFrame(self.format_settings_container, fg_color="transparent")

        stl_size_container = ctk.CTkFrame(self.stl_settings_frame, fg_color="gray20", corner_radius=6)
        stl_size_container.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(stl_size_container, text="Physical Size:",
                    anchor="w", font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=10, pady=(8, 5))

        stl_size_row = ctk.CTkFrame(stl_size_container, fg_color="transparent")
        stl_size_row.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkLabel(stl_size_row, text="W:", width=25).pack(side="left")
        self.stl_width_var = ctk.StringVar(value="150")
        ctk.CTkEntry(stl_size_row, textvariable=self.stl_width_var, width=60, height=28).pack(side="left", padx=(5, 10))

        ctk.CTkLabel(stl_size_row, text="H:", width=25).pack(side="left")
        self.stl_height_mm_var = ctk.StringVar(value="200")
        ctk.CTkEntry(stl_size_row, textvariable=self.stl_height_mm_var, width=60, height=28).pack(side="left", padx=(5, 10))

        ctk.CTkLabel(stl_size_row, text="mm", text_color="gray50").pack(side="left")

        stl_print_container = ctk.CTkFrame(self.stl_settings_frame, fg_color="gray15", corner_radius=6)
        stl_print_container.pack(fill="x", pady=(0, 5))

        ctk.CTkLabel(stl_print_container, text="Print Settings:",
                    anchor="w", font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=10, pady=(5, 3))

        settings_grid = ctk.CTkFrame(stl_print_container, fg_color="transparent")
        settings_grid.pack(fill="x", padx=10, pady=(0, 2))
        settings_grid.grid_columnconfigure(0, weight=1)
        settings_grid.grid_columnconfigure(1, weight=1)

        left_col = ctk.CTkFrame(settings_grid, fg_color="transparent")
        left_col.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        base_row = ctk.CTkFrame(left_col, fg_color="transparent")
        base_row.pack(fill="x", pady=1)
        ctk.CTkLabel(base_row, text="Base:", width=40, anchor="w",
                    font=ctk.CTkFont(size=10)).pack(side="left")
        self.stl_base_var = ctk.StringVar(value="3.0")
        ctk.CTkEntry(base_row, textvariable=self.stl_base_var, width=40, height=22).pack(side="left", padx=2)
        ctk.CTkLabel(base_row, text="mm", text_color="gray50",
                    font=ctk.CTkFont(size=9)).pack(side="left")

        relief_row = ctk.CTkFrame(left_col, fg_color="transparent")
        relief_row.pack(fill="x", pady=1)
        ctk.CTkLabel(relief_row, text="Relief:", width=40, anchor="w",
                    font=ctk.CTkFont(size=10)).pack(side="left")
        self.stl_height_var = ctk.StringVar(value="2.5")
        ctk.CTkEntry(relief_row, textvariable=self.stl_height_var, width=40, height=22).pack(side="left", padx=2)
        ctk.CTkLabel(relief_row, text="mm", text_color="gray50",
                    font=ctk.CTkFont(size=9)).pack(side="left")

        right_col = ctk.CTkFrame(settings_grid, fg_color="transparent")
        right_col.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        res_row = ctk.CTkFrame(right_col, fg_color="transparent")
        res_row.pack(fill="x", pady=1)
        ctk.CTkLabel(res_row, text="Res:", width=40, anchor="w",
                    font=ctk.CTkFont(size=10)).pack(side="left")
        self.stl_res_var = ctk.StringVar(value="800")
        ctk.CTkEntry(res_row, textvariable=self.stl_res_var, width=40, height=22).pack(side="left", padx=2)
        ctk.CTkLabel(res_row, text="px", text_color="gray50",
                    font=ctk.CTkFont(size=9)).pack(side="left")

        smooth_row = ctk.CTkFrame(right_col, fg_color="transparent")
        smooth_row.pack(fill="x", pady=1)
        ctk.CTkLabel(smooth_row, text="Smooth:", width=40, anchor="w",
                    font=ctk.CTkFont(size=10)).pack(side="left")
        self.stl_smooth_var = ctk.StringVar(value="1.0")
        ctk.CTkEntry(smooth_row, textvariable=self.stl_smooth_var, width=40, height=22).pack(side="left", padx=2)

        self.stl_invert_sw = ctk.CTkSwitch(stl_print_container, text=" Invert",
                                           font=ctk.CTkFont(size=10))
        self.stl_invert_sw.pack(anchor="w", padx=10, pady=(2, 4))

        # ========== COLUMN 4: FEATURES & ACTIONS ==========
        col4 = ctk.CTkFrame(controls_container, fg_color="gray20")
        col4.grid(row=1, column=3, sticky="nsew", padx=10, pady=10, ipady=10)

        self.add_column_header(col4, "FEATURES")

        ctk.CTkLabel(col4, text="Presets:", anchor="w", font=ctk.CTkFont(size=10),
                    text_color="gray50").pack(fill="x", padx=15, pady=(0, 5))

        preset_frame = ctk.CTkFrame(col4, fg_color="transparent")
        preset_frame.pack(fill="x", padx=15, pady=(0, 12))

        self.preset_menu = ctk.CTkOptionMenu(
            preset_frame, values=["No presets"],
            command=self.load_preset, width=120
        )
        self.preset_menu.pack(side="left", fill="x", expand=True)
        self.update_presets_menu()

        ctk.CTkButton(
            preset_frame, text="Save", width=30, height=28,
            command=self.save_preset_dialog
        ).pack(side="left", padx=(5, 0))

        toggle_frame = ctk.CTkFrame(col4, fg_color="gray15")
        toggle_frame.pack(fill="x", padx=15, pady=(0, 15))

        self.text_sw = ctk.CTkSwitch(toggle_frame, text=" Text")
        self.text_sw.select()
        self.text_sw.pack(anchor="w", padx=10, pady=8)

        self.road_sw = ctk.CTkSwitch(toggle_frame, text=" Roads")
        self.road_sw.select()
        self.road_sw.pack(anchor="w", padx=10, pady=8)

        self.water_sw = ctk.CTkSwitch(toggle_frame, text=" Water")
        self.water_sw.select()
        self.water_sw.pack(anchor="w", padx=10, pady=8)

        self.park_sw = ctk.CTkSwitch(toggle_frame, text=" Parks")
        self.park_sw.select()
        self.park_sw.pack(anchor="w", padx=10, pady=8)

        self.sea_sw = ctk.CTkSwitch(toggle_frame, text=" Ocean")
        self.sea_sw.pack(anchor="w", padx=10, pady=8)

        self.wetlands_sw = ctk.CTkSwitch(toggle_frame, text=" Wetlands")
        self.wetlands_sw.pack(anchor="w", padx=10, pady=8)

        self.religious_sw = ctk.CTkSwitch(toggle_frame, text=" Religious sites")
        self.religious_sw.pack(anchor="w", padx=10, pady=8)

        self.historic_sw = ctk.CTkSwitch(toggle_frame, text=" Historical sites")
        self.historic_sw.pack(anchor="w", padx=10, pady=8)

        # Batch generation toggle
        self.all_themes_sw = ctk.CTkSwitch(toggle_frame, text=" All Themes")
        self.all_themes_sw.pack(anchor="w", padx=10, pady=8)

        self.gen_btn = ctk.CTkButton(
            col4, text=" GENERATE (Ctrl+G)",
            fg_color="#10b981", hover_color="#059669",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=50, command=self.start_generation
        )
        self.gen_btn.pack(fill="x", padx=15, pady=(0, 10))

        self.folder_btn = ctk.CTkButton(
            col4, text=" Folder (Ctrl+O)",
            fg_color="gray30", hover_color="gray40",
            height=35, command=self.open_output_folder
        )
        self.folder_btn.pack(fill="x", padx=15, pady=(0, 15))

        # ===== BOTTOM SECTION: OUTPUT LOG =====
        log_container = ctk.CTkFrame(self, fg_color="transparent")
        log_container.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        log_container.grid_columnconfigure(0, weight=1)
        log_container.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(log_container, fg_color="gray20", height=50)
        log_header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header, text="STUDIO OUTPUT",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left", padx=20, pady=10)

        self.progress = ctk.CTkProgressBar(log_header, mode="indeterminate", width=200)
        self.progress.pack(side="right", padx=20)
        self.progress.set(0)

        self.log_box = ctk.CTkTextbox(
            log_container, font=("Consolas", 12),
            text_color="#10b981", wrap="word"
        )
        self.log_box.grid(row=1, column=0, sticky="nsew")

    def add_column_header(self, parent: ctk.CTkFrame, text: str) -> None:
        """Add a header to a column."""
        header = ctk.CTkLabel(
            parent, text=text,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="gray15", corner_radius=6, height=35
        )
        header.pack(fill="x", padx=15, pady=(15, 15))

    def toggle_custom_text_sizes(self) -> None:
        """Enable/disable custom text size entry fields."""
        if self.custom_text_size_sw.get():
            self.city_font_size_entry.configure(state="normal")
            self.country_font_size_entry.configure(state="normal")
            self.coords_font_size_entry.configure(state="normal")
        else:
            self.city_font_size_var.set("60")
            self.country_font_size_var.set("30")
            self.coords_font_size_var.set("22")
            self.city_font_size_entry.configure(state="disabled")
            self.country_font_size_entry.configure(state="disabled")
            self.coords_font_size_entry.configure(state="disabled")

    def toggle_location_mode(self) -> None:
        """Switch between city/country and coordinate inputs."""
        if self.location_mode.get() == "city":
            self.coord_frame.pack_forget()
            self.city_frame.pack(fill="x")
        else:
            self.city_frame.pack_forget()
            self.coord_frame.pack(fill="x")

    def set_preset(self, choice: str) -> None:
        """Update dimensions based on resolution preset."""
        if choice in self.resolutions:
            w, h = self.resolutions[choice]
            self.w_var.set(str(w))
            self.h_var.set(str(h))

    def on_format_change(self, format_choice: str) -> None:
        """Show/hide format-specific settings based on format selection."""
        self.png_settings_frame.pack_forget()
        self.svg_settings_frame.pack_forget()
        self.stl_settings_frame.pack_forget()

        if format_choice in ("png", "pdf"):
            self.png_settings_frame.pack(fill="both", expand=True)
        elif format_choice == "svg":
            self.svg_settings_frame.pack(fill="both", expand=True)
        elif format_choice == "stl":
            self.stl_settings_frame.pack(fill="both", expand=True)

    def validate_inputs(self) -> bool:
        """Validate all user inputs before generation."""
        mode = self.location_mode.get()

        if mode == "city":
            city = self.city_entry.get().strip()
            country = self.country_entry.get().strip()
            if not city:
                messagebox.showwarning("Missing Input", "City name is required.")
                return False
            if not country:
                messagebox.showwarning("Missing Input", "Country name is required.")
                return False
        else:
            try:
                lat = float(self.lat_entry.get().strip())
                lon = float(self.lon_entry.get().strip())
                if not -90 <= lat <= 90:
                    messagebox.showwarning("Invalid Latitude", "Latitude must be between -90 and 90.")
                    return False
                if not -180 <= lon <= 180:
                    messagebox.showwarning("Invalid Longitude", "Longitude must be between -180 and 180.")
                    return False
            except ValueError:
                messagebox.showwarning("Invalid Coordinates", "Please enter valid decimal coordinates.")
                return False

        try:
            dist = int(float(self.dist_entry.get()))
            if dist < 1000 or dist > 50000:
                messagebox.showwarning("Invalid Distance", "Distance must be between 1,000 and 50,000 meters.")
                return False
        except ValueError:
            messagebox.showwarning("Invalid Distance", "Distance must be a number.")
            return False

        try:
            w = float(self.w_var.get())
            h = float(self.h_var.get())
            if w <= 0 or h <= 0 or w > 20 or h > 20:
                messagebox.showwarning("Invalid Size", "Dimensions must be between 0 and 20 inches.")
                return False
        except ValueError:
            messagebox.showwarning("Invalid Size", "Width and height must be numbers.")
            return False

        try:
            dpi = int(self.dpi_entry.get())
            if dpi < 72 or dpi > 600:
                messagebox.showwarning("Invalid DPI", "DPI should be between 72 and 600.")
                return False
        except ValueError:
            messagebox.showwarning("Invalid DPI", "DPI must be a number.")
            return False

        return True

    def open_output_folder(self) -> None:
        """Open the posters output directory."""
        output_dir = self.base_path / "posters"
        output_dir.mkdir(exist_ok=True)

        try:
            if sys.platform == "win32":
                os.startfile(output_dir)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(output_dir)])
            else:
                subprocess.run(["xdg-open", str(output_dir)])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder: {e}")

    def _collect_generation_params(self) -> dict:
        """Collect all generation parameters from the GUI form."""
        mode = self.location_mode.get()
        params: dict = {}

        if mode == "city":
            params["city"] = self.city_entry.get().strip()
            params["country"] = self.country_entry.get().strip()
        else:
            params["latitude"] = self.lat_entry.get().strip()
            params["longitude"] = self.lon_entry.get().strip()
            params["city"] = self.city_override_entry.get().strip() or "Custom_Location"
            params["country"] = self.country_override_entry.get().strip() or "Coordinates"

        params["theme"] = self.theme_menu.get()
        params["all_themes"] = bool(self.all_themes_sw.get())
        params["distance"] = int(self.dist_entry.get())
        params["width"] = float(self.w_var.get())
        params["height"] = float(self.h_var.get())
        params["dpi"] = int(self.dpi_entry.get())
        params["font_family"] = self.font_menu.get()
        params["network_type"] = self.network_menu.get()
        params["format"] = self.format_menu.get()
        params["svg_layers"] = bool(self.format_menu.get() == "svg" and self.svg_layers_sw.get())

        # Custom text sizes
        if self.custom_text_size_sw.get():
            try:
                params["city_font_size"] = float(self.city_font_size_var.get())
            except ValueError as e:
                logger.debug("Invalid font size input, skipping: %s", e)
            try:
                params["country_font_size"] = float(self.country_font_size_var.get())
            except ValueError as e:
                logger.debug("Invalid font size input, skipping: %s", e)
            try:
                params["coords_font_size"] = float(self.coords_font_size_var.get())
            except ValueError as e:
                logger.debug("Invalid font size input, skipping: %s", e)

        # STL settings
        if self.format_menu.get() == "stl":
            params["stl_width"] = self.stl_width_var.get()
            params["stl_height_mm"] = self.stl_height_mm_var.get()
            params["stl_base"] = self.stl_base_var.get()
            params["stl_max_height"] = self.stl_height_var.get()
            params["stl_resolution"] = self.stl_res_var.get()
            params["stl_smoothing"] = self.stl_smooth_var.get()
            params["stl_invert"] = bool(self.stl_invert_sw.get())

        # Display overrides
        if mode == "city":
            city_override = self.city_override_entry.get().strip()
            country_override = self.country_override_entry.get().strip()
            if city_override:
                params["display_city"] = city_override
            if country_override:
                params["display_country"] = country_override

        # Feature toggles
        params["no_text"] = not self.text_sw.get()
        params["no_roads"] = not self.road_sw.get()
        params["no_water"] = not self.water_sw.get()
        params["no_parks"] = not self.park_sw.get()
        params["show_sea"] = bool(self.sea_sw.get())
        params["show_wetlands"] = bool(self.wetlands_sw.get())
        params["show_religious"] = bool(self.religious_sw.get())
        params["show_historic"] = bool(self.historic_sw.get())

        return params

    def _build_subprocess_cmd(self, params: dict) -> list[str]:
        """Build a subprocess command list from generation parameters."""
        cmd = [sys.executable, str(self.script_path)]

        if "latitude" in params:
            cmd.extend(["--latitude", params["latitude"], "--longitude", params["longitude"]])
        cmd.extend(["-c", params["city"], "-C", params["country"]])

        if params.get("all_themes"):
            cmd.append("--all-themes")
        else:
            cmd.extend(["-t", params["theme"]])

        cmd.extend([
            "-d", str(params["distance"]),
            "-W", str(params["width"]),
            "-H", str(params["height"]),
            "--dpi", str(params["dpi"]),
            "--font-family", params["font_family"],
            "--network-type", params["network_type"],
            "-f", params["format"],
        ])

        for key, flag in [
            ("city_font_size", "--city-font-size"),
            ("country_font_size", "--country-font-size"),
            ("coords_font_size", "--coords-font-size"),
        ]:
            if key in params:
                cmd.extend([flag, str(params[key])])

        if params.get("svg_layers"):
            cmd.append("--svg-layers")

        if params["format"] == "stl":
            cmd.extend([
                "--stl-width", params["stl_width"],
                "--stl-height", params["stl_height_mm"],
                "--stl-base-thickness", params["stl_base"],
                "--stl-max-height", params["stl_max_height"],
                "--stl-resolution", params["stl_resolution"],
                "--stl-smoothing", params["stl_smoothing"],
            ])
            if params.get("stl_invert"):
                cmd.append("--stl-invert")

        if params.get("display_city"):
            cmd.extend(["-dc", params["display_city"]])
        if params.get("display_country"):
            cmd.extend(["-dC", params["display_country"]])

        if params.get("no_text"):
            cmd.append("--no-text")
        if params.get("no_roads"):
            cmd.append("--no-roads")
        if params.get("no_water"):
            cmd.append("--no-water")
        if params.get("no_parks"):
            cmd.append("--no-parks")
        if params.get("show_sea"):
            cmd.append("--show-sea")
        if params.get("show_wetlands"):
            cmd.append("--show-wetlands")
        if params.get("show_religious"):
            cmd.append("--show-religious")
        if params.get("show_historic"):
            cmd.append("--show-historic")

        return cmd

    def start_generation(self) -> None:
        """Start the poster generation process."""
        if self._generating:
            return
        if not self.validate_inputs():
            return

        mode = self.location_mode.get()
        params = self._collect_generation_params()

        # Log header
        if mode == "city":
            location_str = f"{params['city']}, {params['country']}"
        else:
            location_str = f"{params.get('latitude', '?')}, {params.get('longitude', '?')}"
        self.add_recent_location(self._collect_recent_entry())

        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", f"Initializing: {location_str}\n")
        self.log_box.insert("end", f"Theme: {params['theme']}")
        if params.get("all_themes"):
            self.log_box.insert("end", " (ALL THEMES)")
        self.log_box.insert("end", "\n")
        self.log_box.insert("end", f"Distance: {params['distance']}m\n")

        if params["format"] == "stl":
            self.log_box.insert("end", f"Size: {params.get('stl_width', '?')}mm x {params.get('stl_height_mm', '?')}mm\n")
        else:
            self.log_box.insert("end", f"Size: {params['width']}\" x {params['height']}\" @ {params['dpi']} DPI\n")

        self.log_box.insert("end", f"Format: {params['format'].upper()}\n")
        self.log_box.insert("end", "\n" + "=" * 50 + "\n\n")

        self.save_last_used()
        self._generating = True
        self.gen_btn.configure(
            fg_color="#e53e3e", hover_color="#c53030",
            text=" CANCEL", command=self._cancel_generation
        )
        self.progress.start()

        if getattr(sys, 'frozen', False):
            # Frozen EXE: call create_poster() directly (no subprocess available)
            threading.Thread(target=self._run_direct, args=(params,), daemon=True).start()
        else:
            # Script mode: subprocess for process isolation
            cmd = self._build_subprocess_cmd(params)
            threading.Thread(target=self._run_subprocess, args=(cmd,), daemon=True).start()

    def _restore_stdio(self) -> None:
        """Restore sys.stdout and sys.stderr to their original values."""
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr

    def _reset_gen_btn(self) -> None:
        """Reset the generate button to its default state."""
        self.gen_btn.configure(
            state="normal",
            fg_color="#10b981", hover_color="#059669",
            text=" GENERATE (Ctrl+G)", command=self.start_generation
        )

    def _cancel_generation(self) -> None:
        """Cancel the running generation process."""
        self._generating = False
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self.log_box.insert("end", "\n[CANCELLED] Generation cancelled by user.\n")
        self.log_box.see("end")

    def on_closing(self) -> None:
        """Handle window close: terminate any running subprocess and restore stdio."""
        if self._generating:
            if not messagebox.askyesno(
                "Generation in Progress",
                "A poster is still being generated. Close anyway and cancel it?"
            ):
                return
            self._generating = False
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
        self._restore_stdio()
        self.destroy()

    def _run_direct(self, params: dict) -> None:
        """Run poster generation directly via import (frozen EXE mode)."""
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        class GUIWriter:
            def __init__(self, log_box, after_func):
                self.log_box = log_box
                self.after_func = after_func

            def write(self, text):
                if not text or not text.strip():
                    return
                clean = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)
                clean = clean.replace('\r', '\n')
                for line in clean.split('\n'):
                    line = line.strip()
                    if line and line.count('#') < 10 and line.count('\u2588') < 10:
                        self.after_func(0, lambda t=line: self.log_box.insert("end", t + '\n'))
                        self.after_func(0, lambda: self.log_box.see("end"))

            def flush(self):
                pass

            def isatty(self):
                return False

        try:
            sys.stdout = GUIWriter(self.log_box, self.after)
            sys.stderr = GUIWriter(self.log_box, self.after)

            from create_map_poster import (
                create_poster, load_theme, get_available_themes,
                get_coordinates, generate_output_filename, validate_coordinates,
                load_fonts,
            )
            from lat_lon_parser import parse as parse_coord

            # Resolve coordinates
            if "latitude" in params:
                lat = parse_coord(params["latitude"])
                lon = parse_coord(params["longitude"])
                validate_coordinates(lat, lon)
                coords = (lat, lon)
            else:
                coords = get_coordinates(params["city"], params["country"])

            # Determine themes
            if params.get("all_themes"):
                themes_to_generate = get_available_themes()
            else:
                themes_to_generate = [params["theme"]]

            # Load custom fonts
            custom_fonts = None
            if params.get("font_family"):
                custom_fonts = load_fonts(params["font_family"])

            font_family = params.get("font_family") or "sans-serif"

            # Build STL settings if needed
            stl_settings = None
            if params["format"] == "stl":
                try:
                    from stl_generator import STLSettings
                    stl_settings = STLSettings()
                    stl_settings.width_mm = float(params.get("stl_width", 150))
                    stl_settings.height_mm = float(params.get("stl_height_mm", 200))
                    stl_settings.base_thickness = float(params.get("stl_base", 3.0))
                    stl_settings.max_relief_height = float(params.get("stl_max_height", 2.5))
                    stl_settings.resolution = int(params.get("stl_resolution", 800))
                    stl_settings.smoothing = float(params.get("stl_smoothing", 1.0))
                    stl_settings.invert = params.get("stl_invert", False)
                except ImportError:
                    raise RuntimeError("STL generation requires: pip install trimesh scipy")

            for theme_name in themes_to_generate:
                current_theme = load_theme(theme_name)
                output_file = generate_output_filename(params["city"], theme_name, params["format"])

                create_poster(
                    city=params["city"],
                    country=params["country"],
                    point=coords,
                    dist=params["distance"],
                    output_file=output_file,
                    output_format=params["format"],
                    width=params["width"],
                    height=params["height"],
                    display_city=params.get("display_city"),
                    display_country=params.get("display_country"),
                    fonts=custom_fonts,
                    svg_layers=params.get("svg_layers", False),
                    stl_settings=stl_settings,
                    city_font_size=params.get("city_font_size"),
                    country_font_size=params.get("country_font_size"),
                    coords_font_size=params.get("coords_font_size"),
                    show_text=not params.get("no_text", False),
                    no_roads=params.get("no_roads", False),
                    no_water=params.get("no_water", False),
                    no_parks=params.get("no_parks", False),
                    show_sea=params.get("show_sea", False),
                    show_wetlands=params.get("show_wetlands", False),
                    show_religious=params.get("show_religious", False),
                    show_historic=params.get("show_historic", False),
                    font_family=font_family,
                    theme=current_theme,
                    network_type=params.get("network_type", "all"),
                )

            self.after(0, lambda: self.log_box.insert("end", "\n[OK] SUCCESS!\n"))
            self.after(0, lambda: messagebox.showinfo("Success", "Poster generated successfully!"))

        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda msg=error_msg: self.log_box.insert("end", f"\n[FAIL] Error: {msg}\n"))
            self.after(0, lambda msg=error_msg: messagebox.showerror("Error", f"Error: {msg}"))

        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            self._generating = False
            self.after(0, self.progress.stop)
            self.after(0, self._reset_gen_btn)

    def _run_subprocess(self, cmd: list[str]) -> None:
        """Run the poster generation as a subprocess with real-time output streaming."""
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.base_path),
            )
            self._process = process

            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

            try:
                for line in process.stdout:
                    # Clean ANSI escape codes
                    clean_line = ansi_escape.sub('', line).rstrip()
                    if not clean_line:
                        continue

                    # Filter progress bar lines
                    progress_chars = clean_line.count('#') + clean_line.count('\u2588') + clean_line.count('\u25aa')
                    if progress_chars > 10:
                        percent_match = re.search(r'(\d+)%', clean_line)
                        if percent_match:
                            percent = int(percent_match.group(1))
                            if percent % 20 == 0 or percent == 100:
                                label_match = re.search(r'^([^:]+):', clean_line)
                                if label_match:
                                    self.after(0, lambda t=f"  {label_match.group(1)}: {percent}%":
                                              self.log_box.insert("end", t + '\n'))
                        continue

                    self.after(0, lambda t=clean_line: self.log_box.insert("end", t + '\n'))
                    self.after(0, lambda: self.log_box.see("end"))
            finally:
                process.stdout.close()
                process.wait()

            return_code = process.returncode

            if return_code == 0:
                self.after(0, lambda: self.log_box.insert("end", "\n[OK] SUCCESS!\n"))
                self.after(0, lambda: messagebox.showinfo("Success", "Poster generated successfully!"))
            else:
                self.after(0, lambda rc=return_code: self.log_box.insert("end", f"\n[FAIL] Exit code {rc}\n"))
                self.after(0, lambda rc=return_code: messagebox.showerror("Error", f"Generation failed (exit code {rc})"))

        except FileNotFoundError:
            error_msg = f"Python interpreter not found: {sys.executable}"
            self.after(0, lambda: self.log_box.insert("end", f"\n[FAIL] {error_msg}\n"))
            self.after(0, lambda: messagebox.showerror("Error", error_msg))

        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda msg=error_msg: self.log_box.insert("end", f"\n[FAIL] Error: {msg}\n"))
            self.after(0, lambda msg=error_msg: messagebox.showerror("Error", f"Error: {msg}"))

        finally:
            self._generating = False
            self._process = None
            self.after(0, self.progress.stop)
            self.after(0, self._reset_gen_btn)


if __name__ == "__main__":
    import matplotlib
    matplotlib.use('Agg')

    app = ModernMapPosterGUI()

    import atexit
    import matplotlib.pyplot as plt

    def cleanup_matplotlib() -> None:
        try:
            plt.close('all')
        except Exception:
            pass

    atexit.register(cleanup_matplotlib)

    app.mainloop()
