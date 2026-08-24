/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Ported directly from the Streamlit app's inject_css() :root
      // variables -- not a new palette. The brief for this pass is "match
      // what exists," not "redesign it."
      colors: {
        accent: "#12B886",
        "accent-2": "#6C63FF",
        ink: "#1A1D23",
        bg: "#F6F7F9",
        surface: "#FFFFFF",
        border: "#E4E7EC",
        sidebar: {
          from: "#14162B",
          to: "#1B1E38",
        },
      },
      fontFamily: {
        display: ["Space Grotesk", "sans-serif"],
        sans: ["Inter", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
      borderRadius: {
        md: "10px",
        lg: "14px",
      },
    },
  },
  plugins: [],
};
