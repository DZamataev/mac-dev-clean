import SwiftUI

enum SidebarPage: String, CaseIterable, Identifiable {
    case cleanup = "Cleanup"
    case deepScan = "Deep Scan"
    case tools = "Tool-managed"
    case review = "Review Only"
    case about = "About"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .cleanup: "sparkles"
        case .deepScan: "magnifyingglass.circle"
        case .tools: "wrench.and.screwdriver"
        case .review: "archivebox"
        case .about: "info.circle"
        }
    }

    static func resolved(_ page: SidebarPage?) -> SidebarPage { page ?? .cleanup }

    var startsScanOnFocus: Bool { self == .cleanup }

    func showsScanActivityIndicator(for activity: AppModel.Activity) -> Bool {
        switch self {
        case .cleanup, .review:
            activity == .scanning
        case .deepScan:
            activity.showsDeepScanIndicator
        case .tools:
            activity.showsToolScanIndicator
        case .about:
            false
        }
    }

    func showsPrimaryScanPlaceholder(for activity: AppModel.Activity, hasReport: Bool) -> Bool {
        guard !hasReport else { return false }
        return switch self {
        case .cleanup, .review:
            showsScanActivityIndicator(for: activity)
        case .tools, .deepScan, .about:
            false
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var page: SidebarPage? = .cleanup
    @State private var showsConfirmation = false

    private var selectedPage: SidebarPage { SidebarPage.resolved(page) }

    var body: some View {
        NavigationSplitView {
            List(SidebarPage.allCases, selection: $page) { item in
                Label(item.rawValue, systemImage: item.symbol)
                    .tag(item)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190, max: 230)
            .safeAreaInset(edge: .bottom) {
                sidebarFooter
            }
        } detail: {
            Group {
                if selectedPage == .about {
                    AboutView()
                } else if selectedPage == .deepScan {
                    VStack(spacing: 0) {
                        header
                        Divider()
                        DeepScanView()
                    }
                } else {
                    VStack(spacing: 0) {
                        header
                        Divider()
                        content
                    }
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 900, minHeight: 640)
        .toolbar {
            ToolbarItemGroup {
                if selectedPage != .about && selectedPage != .deepScan && selectedPage != .tools {
                    Button {
                        Task { await model.scan() }
                    } label: {
                        Label("Scan Again", systemImage: "arrow.clockwise")
                    }
                    .disabled(model.isBusy)
                    .help("Scan developer storage again")

                    if selectedPage == .cleanup {
                        Button("Select All") { model.selectAll() }
                            .disabled(model.isBusy || model.groups.isEmpty)
                            .help("Select every cleanable category")
                        Button("Clear") { model.clearSelection() }
                            .disabled(model.isBusy || model.selectedFlags.isEmpty)
                            .help("Clear the cleanup selection")
                        Button {
                            showsConfirmation = true
                        } label: {
                            Label("Clean Selected", systemImage: "trash")
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.red)
                        .disabled(model.isBusy || model.selectedFlags.isEmpty)
                        .help("Clean the selected categories")
                    }
                }
            }
        }
        .confirmationDialog(
            "Clean selected categories?",
            isPresented: $showsConfirmation,
            titleVisibility: .visible
        ) {
            Button("Clean \(model.selectedLocationCount) Locations", role: .destructive) {
                Task { await model.cleanSelected() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This will remove \(model.selectedSummary). Generated caches may be downloaded or rebuilt later.")
        }
        .task(id: selectedPage) {
            guard selectedPage.startsScanOnFocus else { return }
            await model.scanIfNeeded()
        }
    }

    private var sidebarFooter: some View {
        VStack(alignment: .leading, spacing: 8) {
            if selectedPage.showsScanActivityIndicator(for: model.activity) {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Scanning current tab")
            }
            Text(model.activity.message)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.bar)
    }

    private var header: some View {
        VStack(spacing: 14) {
            HStack(spacing: 14) {
                AppLogo(size: 52)
                VStack(alignment: .leading, spacing: 3) {
                    Text("mac-dev-clean")
                        .font(.title2.bold())
                    Text("Mac disk cleanup tool")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            HStack(spacing: 12) {
                SummaryCard(
                    title: "Cleanable",
                    value: model.report?.cleanableTotal ?? "—",
                    symbol: "sparkles",
                    color: .green
                )
                SummaryCard(
                    title: "Review only",
                    value: model.report?.reportOnlyTotal ?? "—",
                    symbol: "archivebox",
                    color: .orange
                )
                SummaryCard(
                    title: "Selected",
                    value: model.selectedFlags.isEmpty ? "None" : model.selectedSummary,
                    symbol: "checkmark.circle",
                    color: .blue
                )
            }

            HStack(spacing: 12) {
                SummaryCard(
                    title: "Free space",
                    value: model.diskSpace?.free ?? "—",
                    symbol: "internaldrive",
                    color: .cyan
                )
                SummaryCard(
                    title: "Total disk size",
                    value: model.diskSpace?.total ?? "—",
                    symbol: "externaldrive",
                    color: .purple
                )
            }

            if let error = model.errorMessage {
                MessageBanner(
                    text: error,
                    symbol: "exclamationmark.triangle.fill",
                    color: .red,
                    onDismiss: model.dismissMessage
                )
            } else if let warning = model.warningMessage {
                MessageBanner(
                    text: warning,
                    symbol: "exclamationmark.circle.fill",
                    color: .orange,
                    onDismiss: model.dismissMessage
                )
            } else if let notice = model.noticeMessage {
                MessageBanner(
                    text: notice,
                    symbol: "checkmark.circle.fill",
                    color: .green,
                    onDismiss: model.dismissMessage
                )
            }
        }
        .padding(20)
    }

    @ViewBuilder
    private var content: some View {
        if selectedPage.showsPrimaryScanPlaceholder(
            for: model.activity,
            hasReport: model.report != nil
        ) {
            ContentUnavailableView {
                Label("Scanning", systemImage: "internaldrive")
            } description: {
                Text("Measuring developer caches and review-only storage…")
            }
        } else {
            switch selectedPage {
            case .review:
                ReviewOnlyView()
            case .tools:
                ToolManagedView()
            case .cleanup:
                CleanupGroupsView()
            case .deepScan, .about:
                EmptyView()
            }
        }
    }
}

struct AboutView: View {
    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                RavenVectorBrandLogo(size: 270)

                VStack(spacing: 6) {
                    Text("mac-dev-clean")
                        .font(.largeTitle.bold())
                    Text("Version \(AppMetadata.version)")
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }

                Text("A utility for finding and safely reclaiming developer storage on macOS.")
                    .font(.title3)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: 520)

                Link(destination: AppMetadata.ravenVectorWebsite) {
                    Label("Visit ravenvector.com", systemImage: "arrow.up.right.square")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                Divider()
                    .frame(maxWidth: 460)

                VStack(spacing: 5) {

                    Text("Source code under the MIT License")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 40)
            .padding(.vertical, 36)
        }
        .navigationTitle("About")
    }
}

struct CleanupGroupsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                if model.groups.isEmpty {
                    ContentUnavailableView(
                        "Nothing to Clean",
                        systemImage: "checkmark.circle",
                        description: Text("Run another scan after developer tools create new caches.")
                    )
                    .padding(.top, 80)
                } else {
                    ForEach(model.groups) { group in
                        CleanupGroupCard(
                            group: group,
                            isSelected: Binding(
                                get: { model.selectedFlags.contains(group.rule.flag) },
                                set: { selected in
                                    if selected {
                                        model.selectedFlags.insert(group.rule.flag)
                                    } else {
                                        model.selectedFlags.remove(group.rule.flag)
                                    }
                                }
                            )
                        )
                    }
                }
            }
            .padding(20)
        }
    }
}

struct CleanupGroupCard: View {
    @EnvironmentObject private var model: AppModel
    let group: CleanupGroup
    @Binding var isSelected: Bool
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Toggle("", isOn: $isSelected)
                    .labelsHidden()
                    .toggleStyle(.checkbox)
                Button(action: toggleExpanded) {
                    HStack(spacing: 12) {
                        Image(systemName: group.rule.symbol)
                            .font(.title3)
                            .foregroundStyle(.tint)
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(group.rule.title)
                                .font(.headline)
                            Text("\(group.items.count) location\(group.items.count == 1 ? "" : "s")")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(group.displaySize)
                            .font(.headline.monospacedDigit())
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .frame(width: 32, height: 32)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .frame(maxWidth: .infinity)
                .buttonStyle(.plain)
                .help(isExpanded ? "Hide locations" : "Show locations")
                .accessibilityLabel("\(isExpanded ? "Collapse" : "Expand") \(group.rule.title)")
            }

            if isExpanded {
                Divider()
                VStack(spacing: 10) {
                    ForEach(group.items) { item in
                        LocationRow(item: item) { model.reveal(item) }
                    }
                }
            }
        }
        .padding(14)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(isSelected ? Color.accentColor.opacity(0.45) : Color.secondary.opacity(0.15))
        }
    }

    private func toggleExpanded() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) {
            isExpanded.toggle()
        }
    }
}

struct LocationRow: View {
    let item: ScanItem
    let reveal: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(item.label)
                        .font(.subheadline.weight(.medium))
                    Text(item.displaySize)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Text(item.displayPath)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                if !item.note.isEmpty {
                    Text(item.note)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
            Button("Reveal", action: reveal)
                .controlSize(.small)
        }
    }
}

struct ToolManagedView: View {
    @EnvironmentObject private var model: AppModel
    @State private var pendingRecommendation: ToolRecommendation?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                overview
                toolScanControls

                if let report = model.toolReport {
                    statuses(report.statuses)
                    recommendations(report.recommendations)
                } else {
                    ContentUnavailableView(
                        "Tool Inventory Not Loaded",
                        systemImage: "wrench.and.screwdriver",
                        description: Text("Start Tool Scan to inspect storage through each tool's own CLI.")
                    )
                    .padding(.top, 50)
                }
            }
            .padding(20)
        }
        .confirmationDialog(
            "Run tool-managed cleanup?",
            isPresented: Binding(
                get: { pendingRecommendation != nil },
                set: { if !$0 { pendingRecommendation = nil } }
            ),
            titleVisibility: .visible,
            presenting: pendingRecommendation
        ) { recommendation in
            Button("Run", role: .destructive) {
                let id = recommendation.id
                pendingRecommendation = nil
                Task { await model.applyTool(id: id) }
            }
            Button("Cancel", role: .cancel) {
                pendingRecommendation = nil
            }
        } message: { recommendation in
            Text(
                "Command:\n\(recommendation.toolAction?.displayCommand ?? "Unavailable")\n\n"
                + "mac-dev-clean will preview and revalidate this action before invoking the tool."
            )
        }
    }

    private var overview: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Tool-managed storage")
                    .font(.title3.bold())
                Text("Inventory and cleanup use each owner's CLI. Nothing is selected automatically.")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(ByteFormatter.string(model.toolReport?.reclaimableTotalBytes ?? 0))
                .font(.title3.monospacedDigit().bold())
                .foregroundStyle(.secondary)
        }
    }

    private var toolScanControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                if model.activity.showsToolScanIndicator {
                    Button("Cancel Scan") { model.cancelToolScan() }
                } else {
                    Button(model.toolScanButtonTitle) {
                        Task { await model.startToolScan() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy)
                    .help(model.manualScanBlockedReason ?? "Inspect storage through each tool's own CLI")
                }

                if model.activity.showsToolScanIndicator {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Scanning tool-managed storage")
                    Text(model.activity.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            if model.toolScanWasCancelled {
                Text("Scan cancelled.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            if !model.activity.showsToolScanIndicator, let reason = model.manualScanBlockedReason {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func statuses(_ statuses: [ToolStatus]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Availability")
                .font(.headline)
            ForEach(statuses) { status in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: status.available ? "checkmark.circle.fill" : "minus.circle")
                        .foregroundStyle(status.available ? .green : .secondary)
                    Text(status.tool.capitalized)
                        .font(.subheadline.weight(.medium))
                    Spacer()
                    if !status.available {
                        Text(status.reason.isEmpty ? "Unavailable" : status.reason)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                    }
                }
            }
        }
        .padding(14)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
    }

    @ViewBuilder
    private func recommendations(_ recommendations: [ToolRecommendation]) -> some View {
        if recommendations.isEmpty {
            ContentUnavailableView(
                "Nothing Reclaimable",
                systemImage: "checkmark.circle",
                description: Text("The available tools did not report a supported cleanup action.")
            )
            .padding(.top, 36)
        } else {
            Text("Recommendations")
                .font(.headline)
            ForEach(recommendations) { recommendation in
                recommendationRow(recommendation)
            }
        }
    }

    private func recommendationRow(_ recommendation: ToolRecommendation) -> some View {
        ToolRecommendationRow(
            recommendation: recommendation,
            isBusy: model.isBusy
        ) {
            pendingRecommendation = recommendation
        }
    }
}

private struct ToolRecommendationRow: View {
    let recommendation: ToolRecommendation
    let isBusy: Bool
    let run: () -> Void
    @State private var isUsageExpanded = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(recommendation.label)
                        .font(.headline)
                    Spacer()
                    Text(recommendation.size)
                        .font(.headline.monospacedDigit())
                }
                Text(recommendation.reason)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if let usageSummary = recommendation.usageSummary {
                    Text(usageSummary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !recommendation.usageSections.isEmpty {
                    DisclosureGroup("Project usage", isExpanded: $isUsageExpanded) {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(recommendation.usageSections, id: \.title) { section in
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(section.title)
                                        .font(.caption.weight(.semibold))
                                    ForEach(section.paths, id: \.self) { path in
                                        Text(path)
                                            .font(.caption.monospaced())
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                            .truncationMode(.middle)
                                            .textSelection(.enabled)
                                    }
                                }
                            }
                        }
                        .padding(.top, 4)
                    }
                    .font(.caption)
                }
                Text(recommendation.toolAction?.displayCommand ?? "Command unavailable")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                if !recommendation.warning.isEmpty {
                    Text(recommendation.warning)
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button("Run", action: run)
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .disabled(isBusy || recommendation.toolAction == nil)
        }
        .padding(14)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.secondary.opacity(0.15))
        }
    }
}

struct ReviewOnlyView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Keep, archive, or remove manually")
                            .font(.headline)
                        Text("These items are never included in automatic cleanup.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Open iCloud Drive") { model.openICloudDrive() }
                }
                .padding(.bottom, 4)

                ForEach(model.reviewItems) { item in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.label)
                                    .font(.headline)
                                Text(item.category)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(item.displaySize)
                                .font(.headline.monospacedDigit())
                        }
                        Text(item.displayPath)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        if !item.note.isEmpty {
                            Text(item.note)
                                .foregroundStyle(.secondary)
                        }
                        HStack {
                            Spacer()
                            Button("Reveal in Finder") { model.reveal(item) }
                        }
                    }
                    .padding(14)
                    .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12))
                    .overlay {
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color.secondary.opacity(0.15))
                    }
                }
            }
            .padding(20)
        }
    }
}

struct SummaryCard: View {
    let title: String
    let value: String
    let symbol: String
    let color: Color

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(color)
                .font(.title3)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.headline.monospacedDigit())
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}

struct MessageBanner: View {
    let text: String
    let symbol: String
    let color: Color
    let onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(color)
            Text(text)
                .font(.callout)
                .textSelection(.enabled)
            Spacer()
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.caption.weight(.semibold))
                    .frame(width: 20, height: 20)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Dismiss message")
            .accessibilityLabel("Dismiss message")
        }
        .padding(10)
        .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct DeepScanView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showsConfirmation = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            controls
            Divider()
            if model.deepScanState.items.isEmpty {
                emptyState
            } else {
                List {
                    ForEach(model.deepScanState.items) { item in
                        row(for: item)
                    }
                }
                .listStyle(.inset)
            }
        }
        .confirmationDialog(
            "Remove selected project artifacts?",
            isPresented: $showsConfirmation,
            titleVisibility: .visible
        ) {
            Button("Remove \(model.deepScanState.selectedIds.count) Item(s)", role: .destructive) {
                Task { await model.applyDeepScanSelection() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "This removes \(model.deepScanSelectionSummary) of build output and dependencies. "
                + "Source files, manifests, and lock files are never touched. "
                + "Close Xcode, Android Studio, and any running build first."
            )
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                if model.deepScanState.isRunning {
                    Button("Cancel Scan") { model.cancelDeepScan() }
                } else {
                    Button("Start Deep Scan") {
                        Task { await model.startDeepScan() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy)
                    .help(model.manualScanBlockedReason ?? "Analyse Git projects for removable build output")
                }
                Button("Remove Selected") { showsConfirmation = true }
                    .disabled(model.isBusy || model.deepScanState.selectedIds.isEmpty)
                Spacer()
                Text("Selected: \(model.deepScanSelectionSummary)")
                    .font(.callout.monospacedDigit())
            }

            if model.activity.showsDeepScanIndicator {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Scanning projects")
                    Text(model.activity.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !model.deepScanState.currentPath.isEmpty {
                    Text(model.deepScanState.currentPath)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }

            if model.deepScanState.wasCancelled {
                Text("Scan cancelled. Partial results are shown and were not saved for cleanup.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            if !model.deepScanState.isRunning, let reason = model.manualScanBlockedReason {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            ForEach(model.deepScanState.warnings) { warning in
                Text("\(warning.message) \(warning.path)")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding()
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Text("No project artifacts analysed yet.")
                .foregroundStyle(.secondary)
            Text("Deep Scan looks for Git projects that have not changed in 90 days.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private func row(for item: RecommendationItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Toggle(
                isOn: Binding(
                    get: { model.deepScanState.selectedIds.contains(item.id) },
                    set: { _ in model.toggleDeepScanItem(item.id) }
                )
            ) {
                EmptyView()
            }
            .labelsHidden()
            .disabled(model.isBusy)

            VStack(alignment: .leading, spacing: 3) {
                Text(item.label)
                    .font(.headline)
                Text(item.displayPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(item.reason)
                    .font(.caption)
                Text(item.restorationSummary)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Text(item.size)
                .font(.callout.monospacedDigit())
        }
        .padding(.vertical, 4)
    }
}
