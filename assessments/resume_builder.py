import io
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_candidate_resume_pdf(candidate_name, email, phone="", role="Software Engineer", experience_level="Senior"):
    """
    Generate a professional, fully compliant, 1-page PDF resume using ReportLab.
    Returns bytes of the generated PDF document.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=40,
        rightMargin=40,
        topMargin=36,
        bottomMargin=36,
        title=f"Resume - {candidate_name}",
        author=candidate_name,
    )

    styles = getSampleStyleSheet()

    # Custom typography styles
    name_style = ParagraphStyle(
        "ResumeName",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=2,
    )
    role_style = ParagraphStyle(
        "ResumeRole",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0D9488"),
        spaceAfter=4,
    )
    contact_style = ParagraphStyle(
        "ResumeContact",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#64748B"),
        spaceAfter=8,
    )
    section_hdr = ParagraphStyle(
        "ResumeSectionHdr",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=8,
        spaceAfter=3,
        keepWithNext=True,
    )
    job_title = ParagraphStyle(
        "ResumeJobTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#1E293B"),
    )
    job_meta = ParagraphStyle(
        "ResumeJobMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#64748B"),
        alignment=2,  # Right-aligned
    )
    body_style = ParagraphStyle(
        "ResumeBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155"),
        spaceAfter=2,
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155"),
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=1.5,
    )

    story = []

    # 1. Header (Name, Title, Contact Info)
    story.append(Paragraph(candidate_name, name_style))
    story.append(Paragraph(role, role_style))
    contact_line = f"Email: {email}"
    if phone:
        contact_line += f" &nbsp;|&nbsp; Mobile: {phone}"
    contact_line += " &nbsp;|&nbsp; Location: San Francisco, CA &nbsp;|&nbsp; Portfolio: github.com/profile"
    story.append(Paragraph(contact_line, contact_style))

    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CBD5E1"), spaceBefore=2, spaceAfter=6))

    # 2. Executive Summary
    story.append(Paragraph("PROFESSIONAL SUMMARY", section_hdr))
    summary_text = (
        f"Accomplished {experience_level.lower()} software engineer with proven expertise in architecting resilient backend systems, "
        "scalable web applications, and low-latency APIs. Demonstrated leadership in delivering mission-critical cloud "
        "solutions, optimizing performance bottlenecks, and writing clean, maintainable, test-driven code."
    )
    story.append(Paragraph(summary_text, body_style))
    story.append(Spacer(1, 4))

    # 3. Technical Core Competencies
    story.append(Paragraph("TECHNICAL EXPERTISE", section_hdr))
    skills_data = [
        [
            Paragraph("<b>Languages:</b> Python, JavaScript, TypeScript, Go, SQL", body_style),
            Paragraph("<b>Databases:</b> PostgreSQL, Redis, MySQL, SQLite", body_style),
        ],
        [
            Paragraph("<b>Frameworks:</b> Django, FastAPI, React, Next.js, Node.js", body_style),
            Paragraph("<b>DevOps &amp; Cloud:</b> Docker, Kubernetes, AWS, CI/CD, Git", body_style),
        ],
    ]
    t = Table(skills_data, colWidths=[260, 272])
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    story.append(t)
    story.append(Spacer(1, 4))

    # 4. Work Experience
    story.append(Paragraph("WORK EXPERIENCE", section_hdr))

    # Job 1
    job1_header = [
        [
            Paragraph(f"<b>{role}</b> &bull; Apex Innovations Corp", job_title),
            Paragraph("2023 &ndash; Present", job_meta),
        ]
    ]
    t_job1 = Table(job1_header, colWidths=[380, 152])
    t_job1.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.append(t_job1)
    story.append(Spacer(1, 2))
    story.append(Paragraph("&bull; Architected distributed asynchronous task pipelines processing over 3M events daily with 99.99% uptime.", bullet_style))
    story.append(Paragraph("&bull; Reduced p99 database query response latency by 42% through query index tuning and Redis caching strategies.", bullet_style))
    story.append(Paragraph("&bull; Mentored 4 junior engineers on design patterns, code review standards, and automated regression testing.", bullet_style))
    story.append(Spacer(1, 4))

    # Job 2
    job2_header = [
        [
            Paragraph("<b>Software Engineer</b> &bull; Nexus Global Systems", job_title),
            Paragraph("2020 &ndash; 2023", job_meta),
        ]
    ]
    t_job2 = Table(job2_header, colWidths=[380, 152])
    t_job2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.append(t_job2)
    story.append(Spacer(1, 2))
    story.append(Paragraph("&bull; Designed secure RESTful APIs with fine-grained role-based access control (RBAC) and OAuth2 authentication.", bullet_style))
    story.append(Paragraph("&bull; Automated continuous delivery pipelines using GitHub Actions, decreasing build-to-deploy duration from 45 to 8 minutes.", bullet_style))
    story.append(Paragraph("&bull; Spearheaded comprehensive unit and integration test coverage across the core business logic layer.", bullet_style))
    story.append(Spacer(1, 4))

    # 5. Education & Certifications
    story.append(Paragraph("EDUCATION &amp; CERTIFICATIONS", section_hdr))
    edu_header = [
        [
            Paragraph("<b>B.S. in Computer Science &amp; Engineering</b> &bull; State Technical University", job_title),
            Paragraph("Graduated Magna Cum Laude", job_meta),
        ]
    ]
    t_edu = Table(edu_header, colWidths=[380, 152])
    t_edu.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.append(t_edu)
    story.append(Spacer(1, 2))
    story.append(Paragraph("&bull; AWS Certified Solutions Architect &ndash; Associate (2024)", bullet_style))
    story.append(Paragraph("&bull; Certified Kubernetes Application Developer (CKAD)", bullet_style))

    doc.build(story)
    return buf.getvalue()
